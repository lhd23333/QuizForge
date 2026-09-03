"""文件系统题库存储层，替代 quizbank 的 db.py。

设计要点（对应已批准的方案）：
- 每题一个 .md 文件：YAML frontmatter（ruamel 往返解析，未知字段原样保留）+
  Markdown 正文。正文里 `## 解析` 之后的内容识别为解析，其余 `## 标题` 视为
  用户自定义分区，原样传回、绝不解析或丢弃。
- 文件夹 = data/bank/ 下的真实目录；文件夹 id 即相对 BANK_DIR 的 POSIX 相对路径。
- 回收站 = data/bank/.trash/：单题软删平铺存放（用 frontmatter 记原路径供恢复）；
  整个文件夹被删除时，其子树整体移入 .trash/ 下（附 .trash_meta.json 记原位置）。
- 勾选（选入组卷篮）以题目 id 落在应用 data 目录，插件重启后继续保留。
- 图片：桌面版多题库可共用一个显式图片目录，源码模式默认仍是
  `data/bank/_assets/`；正文统一用 Obsidian 双链 `![[<id>_N.ext]]` 引用。

本模块函数名尽量对齐 quizbank/db.py，方便照原样移植 app.py 的路由逻辑。
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import shutil
import hashlib
import threading
import time
import uuid
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path, PurePosixPath

from ruamel.yaml import YAML

import config
import dedup
from search_query import SearchQuery, matches_search, parse_search_query

logger = logging.getLogger(__name__)

_yaml = YAML()
_yaml.preserve_quotes = True
_yaml.width = 4096  # 别让长题干/公式被自动折行

# 单题元数据更新可能被 rename_tag/add_tags_to 等批量入口嵌套调用；RLock 让所有
# “读当前文件→改一部分→写回”都能共用同一把锁，同时与整组安全刷新互斥。
_write_lock = threading.RLock()

# 正文分区标题行：形如 "## 解析"、"## 备注"
_SECTION_RE = re.compile(r"(?m)^##[ \t]+(.*?)[ \t]*$")
_IMAGE_REF_RE = re.compile(r"!\[\[([^\]\|]+)(?:\|[^\]]*)?\]\]")
_GENERATED_IMAGE_VERSION_RE = re.compile(
    r"\A(?:redraw|tikz)_[0-9a-f]{16}\.(?:png|svg)\Z")

# 内存索引：文件路径(str) -> 已解析记录（含 _path/_mtime）。按 mtime 判断是否需要重解析，
# 使直接在 Obsidian 里改文件也能被下次请求感知到。
_cache: dict[str, dict] = {}

# 轻量题卡身份缓存：绝对路径 -> (mtime_ns, size, id, 是否题卡)。无限滚动首屏只需
# 知道哪些 Markdown 是题卡，不该每次都重新打开三万份文件。mtime_ns + size 让
# Obsidian 外部修改自动失效；独立锁允许路径快照用线程池并发读取未命中的文件头。
_identity_cache: dict[str, tuple[int, int, str | None, bool]] = {}
_identity_cache_lock = threading.RLock()
_IDENTITY_SCAN_WORKERS = 32
_QUESTION_PATHS_CACHE_SECONDS = 5.0
_question_paths_snapshot: dict[
    tuple[str, str], tuple[float, tuple[str, ...]]
] = {}
_question_paths_generation = 0

# 一次页面请求会连续读取题目、标签和文件夹，用户随后点进试卷时也会立刻再读一遍。
# Windows 上对上万份 Markdown 逐个 rglob/stat 需要数秒，因此短时间复用整次扫描；
# 本程序的写入口会主动失效，Obsidian 外部编辑则最多等这个很短的窗口后被发现。
_SCAN_CACHE_SECONDS = 5.0
_SIDEBAR_CACHE_SECONDS = 30.0
_scan_snapshot: dict[str, dict] | None = None
_scan_snapshot_root: Path | None = None
_scan_snapshot_at = 0.0
_tree_snapshot: list[dict] | None = None
_tree_snapshot_root: Path | None = None
_tree_snapshot_at = 0.0
_tags_snapshot: list[str] | None = None
_tags_snapshot_root: Path | None = None
_tags_snapshot_at = 0.0
_scan_lock = threading.RLock()

# 勾选篮：内存集合负责快速查询，修改后原子写入 data/selections.json。
_selected: set[str] = set()
_selected_lock = threading.RLock()

# 这些目录属于 QuizForge 的配套数据，不是题集。集中维护，避免题目扫描、完整目录
# 树和惰性目录树三处各写一套条件，新增讲义目录后只漏掉其中一处。
_RESERVED_BANK_DIRS = frozenset({"_assets", "_handouts", "_backups"})


def invalidate_scan_cache(*, folder_structure: bool = False) -> None:
    """失效题目/标签快照；只有目录结构真变了才失效完整文件夹树。

    题型、难度、分栏等单题写入不会增删目录。旧实现却连 642 个目录的树缓存一起
    清掉，结果是刚点完任意题卡按钮再进“批量导入”，又要重新遍历整棵目录树。
    文件夹创建、改名、移动、删除等调用方显式传 ``folder_structure=True``。
    """
    global _scan_snapshot, _scan_snapshot_root, _scan_snapshot_at
    global _tree_snapshot, _tree_snapshot_root, _tree_snapshot_at
    global _tags_snapshot, _tags_snapshot_root, _tags_snapshot_at
    global _question_paths_generation
    with _scan_lock:
        _scan_snapshot = None
        _scan_snapshot_root = None
        _scan_snapshot_at = 0.0
        if folder_structure:
            _tree_snapshot = None
            _tree_snapshot_root = None
            _tree_snapshot_at = 0.0
        _tags_snapshot = None
        _tags_snapshot_root = None
        _tags_snapshot_at = 0.0
        # 题卡新增、移动、改类型都会改变无限滚动路径快照。身份缓存仍按文件指纹
        # 复用，只清结果快照；generation 防止并发中的旧扫描在这里清空后重新写回。
        with _identity_cache_lock:
            _question_paths_snapshot.clear()
            _question_paths_generation += 1


def _save_selected_unlocked() -> None:
    path = config.SELECTIONS_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as f:
        json.dump(sorted(_selected), f, ensure_ascii=False, separators=(",", ":"))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _load_selected() -> None:
    path = config.SELECTIONS_PATH
    if not path.exists():
        return
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("选题状态无法读取，已忽略：%s", exc)
        return
    if isinstance(raw, list):
        _selected.update(str(qid) for qid in raw if qid)


def init_store():
    """确保题库/回收站/图片目录存在（幂等）。"""
    config.BANK_DIR.mkdir(parents=True, exist_ok=True)
    config.TRASH_DIR.mkdir(parents=True, exist_ok=True)
    config.ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    with _selected_lock:
        _selected.clear()
        _load_selected()


def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


# ---------------------------------------------------------------------------
# 正文分区：stem（题干，首个 `## ` 之前） / 解析 / 其余用户自定义分区（原样透传）
# ---------------------------------------------------------------------------


def _split_sections(body: str) -> tuple[str, list[tuple[str, str]]]:
    """把正文切成 (题干, [(分区标题, 分区内容), ...])。

    **题干绝不会因为正文以 `## ` 开头而变成空**（2026-08-09 补的护栏）。原先
    「首个 `## ` 之前的内容即题干」这条规则遇上「正文第一行就是 `## 标题`」时，
    题干是空字符串、全部内容落进 `sections`——而题卡（app.py 的 qbody 过滤器）
    与 PDF 导出（exporter）都只读题干，两边一起空白，用户看到的是「md 里明明
    有内容，题卡却是空的」。真实成因是 MinerU 把原卷题号提成了 `## ` 标题、
    而剥题号的正则当时不认 `$\\displaystyle N$` 包裹（见 importer._STRIP_NUM_RE），
    一次导入 443 题里 321 题栽在这儿。

    上游那条正则已经补好，这里仍留护栏：`## ` 开头的正文还可能来自用户在
    Obsidian 里手写、或从别处迁入的历史数据，而「静默丢掉整道题的正文」这个
    后果太重，不该只靠一条上游正则挡住。

    护栏的口径刻意窄：只在**题干为空**时把首个分区提回题干，且首个分区是
    `## 解析` 时不动——那是正常的「无题干纯解析」形态（回收站里的残题、
    只录了答案的题），提上来会让解析被当成题干显示。
    """
    matches = list(_SECTION_RE.finditer(body))
    if not matches:
        return body.strip("\n"), []
    stem = body[: matches[0].start()].strip("\n")
    sections = []
    for i, m in enumerate(matches):
        heading = m.group(1).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        content = body[start:end].strip("\n")
        sections.append((heading, content))
    if not stem and sections and sections[0][0] != "解析":
        heading, content = sections[0]
        # 标题连同其下的内容一起回到题干：标题行本身多半就是被误提的题号/章节名，
        # 但也可能是用户手写的小标题，删掉就是改用户的正文。原样保留、只是不再
        # 当分区看——渲染成 `## …` 一行，用户看得见也删得掉。
        stem = f"## {heading}\n{content}".strip("\n") if content else f"## {heading}"
        sections = sections[1:]
    return stem, sections


def _join_sections(stem: str, solution: str | None,
                    extra: list[tuple[str, str]]) -> str:
    """把 (题干, 解析, 其余分区) 拼回正文文本。"""
    parts = [stem.rstrip("\n")]
    if solution and solution.strip():
        # OCR 解析偶尔自带 Markdown 标题。若直接套上题库的 `## 解析` 分区，会
        # 形成两个相邻标题，反读时整段解析被误判为额外分区。Doc2X 还会输出
        # `## 【答案】 BD`：这里只去掉 Markdown 标题语义，行内答案必须留下。
        clean_solution = solution.strip("\n")
        heading = re.match(
            r"\A[ \t]{0,3}#{1,6}[ \t]*"
            r"(?P<label>(?:【[ \t]*)?(?:解析|答案(?:与解析)?)(?:[ \t]*】)?)"
            r"(?P<tail>[^\r\n]*)(?:\r?\n)?",
            clean_solution,
        )
        if heading:
            tail = heading.group("tail").strip()
            rest = clean_solution[heading.end():]
            clean_solution = rest
            if tail:
                label = heading.group("label").strip()
                clean_solution = f"{label} {tail}" + (f"\n{rest}" if rest else "")
        if clean_solution.strip():
            parts.append("## 解析\n" + clean_solution)
    for heading, content in extra:
        parts.append(f"## {heading}\n{content}")
    return "\n\n".join(p for p in parts if p) + "\n"


def _replace_note_section(extra: list[tuple[str, str]], note: str) -> list[tuple[str, str]]:
    """替换首个备注分区并去掉重复备注，其余用户自定义分区保持原位。"""
    clean_note = str(note or "").strip()
    out: list[tuple[str, str]] = []
    inserted = False
    for heading, content in extra:
        if heading != "备注":
            out.append((heading, content))
            continue
        if not inserted and clean_note:
            out.append(("备注", clean_note))
        inserted = True
    if not inserted and clean_note:
        out.append(("备注", clean_note))
    return out


# ---------------------------------------------------------------------------
# frontmatter 读写
# ---------------------------------------------------------------------------

_FM_RE = re.compile(r"(?s)\A---\r?\n(.*?)\r?\n---\r?\n?(.*)\Z")

_QUESTION_MARKDOWN_KIND = "question"


def _is_question_meta(meta) -> bool:
    """判断 Markdown 元数据是否属于题卡。

    旧题卡没有身份字段，必须继续兼容；一旦显式写入 ``quizforge_kind``，则只有
    ``question`` 能进入题库。资料文档、讲义及未来新增类型因此不会被误当成题目。
    """
    if not isinstance(meta, Mapping):
        return False
    if "quizforge_kind" not in meta:
        return True
    return (str(meta.get("quizforge_kind") or "").strip().casefold()
            == _QUESTION_MARKDOWN_KIND)


def _cached_question_identity(
        path: Path, stat: os.stat_result) -> tuple[str | None, bool] | None:
    """返回与当前文件指纹一致的身份缓存；旧指纹立即淘汰。"""
    key = str(path)
    signature = (stat.st_mtime_ns, stat.st_size)
    with _identity_cache_lock:
        cached = _identity_cache.get(key)
        if cached is None:
            return None
        if cached[:2] != signature:
            _identity_cache.pop(key, None)
            return None
        return cached[2], cached[3]


def _has_question_identity_cache(path: Path) -> bool:
    """只判断路径是否曾缓存；命中者才值得单独 stat 校验指纹。"""
    with _identity_cache_lock:
        return str(path) in _identity_cache


def _remember_question_identity(path: Path, stat: os.stat_result,
                                qid: str | None, is_question: bool) -> None:
    """记录一次确定的身份判断；调用方必须传读取前取得的同一份 stat。"""
    with _identity_cache_lock:
        _identity_cache[str(path)] = (
            stat.st_mtime_ns, stat.st_size, qid, bool(is_question))

# frontmatter 里除了下列已知字段，其余字段（用户手写的自定义字段）原样保留，
# 靠 ruamel 的 round-trip 能力自动做到，无需在代码里枚举。
_KNOWN_DEFAULTS = {
    "quizforge_kind": _QUESTION_MARKDOWN_KIND,
    "type": "",
    "difficulty": "",
    "source": "",
    "tags": [],
    "starred": False,
    "order": 0.0,
    "img_align": "",
    "img_width": None,
    # 图文分栏：新题一律 None（=「用户从未设过」），好让 exporter.resolve_split /
    # plan_figs 的默认值生效。写成 "" 会被 _to_record 读成「没设过」也行，但
    # None 更直白，且与线上版的 SQL NULL 同义。明确关掉存 "off"，见 set_img_split。
    "img_split": None,
    "img_layouts": [],
    # 解析图文分栏独立于题干：None=从未设置（默认关闭），full=左文右图，
    # off=用户明确关闭。不能复用 img_split，否则调整解析会改坏题干版式。
    "sol_img_split": None,
    # 解析里的图片逐图排版设置。与 img_layouts 同构，但**序号各自独立编号**
    # ——解析的第 0 张图不是题干的第 0 张图，两张表不能合并。
    "sol_img_layouts": [],
    # AI 重绘的原图备份表：[{"i": 0, "orig": "q123_0.jpg"}]。
    # 有这一项就说明第 i 张图是重绘产物、可以退回原图（前端据此点亮"还原原图"）。
    "img_originals": [],
    # AI 重绘的全部图片版本：[{"i": 0, "name": "...png", "kind": "generated"}]
    # 旧题没有这一项时，由 _to_record 从正文和 img_originals 兼容补齐。
    "img_versions": [],
    # 原卷题号（`importer.block_number` 的产物，无题号/手工新增时为 None）。
    # 以前这个数字在 `_build_import_preview` 里算完做漏题检测就被丢掉了，正文里的
    # 题号也被 `strip_leading_number` 剥掉——结果是入库之后再也无从得知这道题在原卷
    # 里排第几。落进 frontmatter 才能让文件名按题号取（见 `_question_filename`），
    # 也让 Obsidian 侧能直接看出来。
    "number": None,
}


def _as_number(raw) -> int | None:
    """frontmatter 的 `number` 读成 int，读不出来当「没有题号」。

    宽松收而不是抛：这个字段是**用户可以在 Obsidian 里直接手改**的（题库根就是
    vault），填了 `十七` 或者一句话时不该让整道题从列表里消失——`_scan` 里
    `_to_record` 抛异常就是那个后果。
    """
    if raw is None or isinstance(raw, bool):
        return None
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


def normalize_newlines(text: str) -> str:
    """把任意行尾（CRLF / 单独 CR）统一成 LF。

    **每一处「外来文本 → 落盘」的入口都必须先过这里**，理由见 `_write_raw`。
    幂等，对已是 LF 的文本是恒等变换。
    """
    if not text:
        return text
    return text.replace("\r\n", "\n").replace("\r", "\n")


def write_markdown_text(path: Path, text: str, expected_mtime: int) -> tuple[bool, int]:
    """原子保存资料库 Markdown，并用 mtime 拒绝覆盖外部修改。

    ``path`` 必须先由路由层验证位于题库根目录；这里共享题目写锁，是为了避免
    资料库源码编辑与题卡编辑同时替换同一文件。正文仍统一收敛为 LF。
    """
    normalized = normalize_newlines(text)
    with _write_lock:
        current = path.stat().st_mtime_ns
        if current != expected_mtime:
            return False, current
        tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with tmp.open("w", encoding="utf-8", newline="\n") as handle:
                handle.write(normalized)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)
        new_mtime = path.stat().st_mtime_ns
        _cache.pop(str(path), None)
        invalidate_scan_cache()
        return True, new_mtime


def create_markdown_text(path: Path, text: str) -> int:
    """独占创建一份普通 Markdown，返回 ``st_mtime_ns``。

    讲义新建/另存为也必须和题卡、资料库保存共用同一把写锁。这里使用 ``x`` 模式
    保证撞名时绝不覆盖已有文件；新文件内容不经 frontmatter 解析，只统一 LF。
    """
    normalized = normalize_newlines(text)
    with _write_lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(normalized)
            handle.flush()
            os.fsync(handle.fileno())
        invalidate_scan_cache(folder_structure=True)
        return path.stat().st_mtime_ns


def _parse_raw_text(text: str) -> tuple[dict, str]:
    """解析已经从磁盘取得的 Markdown 文本，供原子校验复用同一份字节。"""
    text = normalize_newlines(text)
    m = _FM_RE.match(text)
    if not m:
        return {}, text
    fm_text, body = m.group(1), m.group(2)
    data = _yaml.load(fm_text) or {}
    return data, body


def _read_raw(path: Path) -> tuple[dict, str]:
    # newline="" 关掉 universal newlines：读进来是磁盘上的原始行尾，不会把
    # 病态的 \r\r\n 认成两个换行。归一由 normalize_newlines 显式做，好让
    # 「行尾长什么样」这件事只有一处说法。
    text = path.read_text(encoding="utf-8", newline="")
    return _parse_raw_text(text)


def _render_raw(meta: dict, body: str) -> str:
    """把 frontmatter 与正文序列化成统一 LF 文本，不触碰磁盘。"""
    import io

    buf = io.StringIO()
    _yaml.dump(meta, buf)
    text = "---\n" + buf.getvalue() + "---\n\n" + body.lstrip("\n")
    # 落盘前再归一一次：body 可能来自浏览器 textarea（HTML 规范提交 CRLF）、
    # 也可能来自用户在 Obsidian 里手改过的文件。
    return normalize_newlines(text)


def _write_raw(path: Path, meta: dict, body: str):
    text = _render_raw(meta, body)
    parent_existed = path.parent.is_dir()
    path.parent.mkdir(parents=True, exist_ok=True)
    # newline="\n" 是这里的关键，**不是可省的讲究**：默认文本模式会把每个 \n
    # 翻成 os.linesep（Windows 上 \r\n），文本里原有的 \r 于是变成 \r\r\n；
    # 读回来时 universal newlines 又把 \r\r\n 认成**两个**换行——一个空行每
    # 存一轮翻一倍，解答题小问之间就这么空出三行（2026-08-08 定位）。
    # 线上版存进 SQLite，没有这层翻译，所以同一份识别结果两版产物不同。
    path.write_text(text, encoding="utf-8", newline="\n")
    invalidate_scan_cache(folder_structure=not parent_existed)


def _image_version_kind(name: str, requested: str = "") -> str:
    """把版本标成原图或 AI 生成物；未知 kind 只按文件名保守推断。"""
    if requested in {"original", "generated"}:
        return requested
    return ("generated" if _GENERATED_IMAGE_VERSION_RE.fullmatch(name or "")
            else "original")


def _merge_img_versions(meta: Mapping, body: str) -> list[dict]:
    """合并新版本表与旧的原图记录，兼容尚未迁移的题目。"""
    versions: list[dict] = []
    seen: set[tuple[int, str]] = set()

    def add(index, name, *, kind="", created="", model="", prompt=""):
        try:
            i = int(index)
        except (TypeError, ValueError):
            return
        name = str(name or "").strip()
        if i < 0 or not name or "/" in name or "\\" in name or name.startswith("."):
            return
        key = (i, name)
        if key in seen:
            return
        seen.add(key)
        row = {"i": i, "name": name,
               "kind": _image_version_kind(name, str(kind or ""))}
        if created:
            row["created"] = str(created)[:40]
        if model:
            row["model"] = str(model)[:120]
        if prompt:
            row["prompt"] = str(prompt)[:2000]
        versions.append(row)

    for item in meta.get("img_versions", []) or []:
        if isinstance(item, Mapping):
            add(item.get("i"), item.get("name"), kind=item.get("kind", ""),
                created=item.get("created", ""), model=item.get("model", ""),
                prompt=item.get("prompt", ""))
    for item in meta.get("img_originals", []) or []:
        if isinstance(item, Mapping):
            add(item.get("i"), item.get("orig"), kind="original",
                created=meta.get("created", ""))
    for i, match in enumerate(_IMAGE_REF_RE.finditer(body or "")):
        name = match.group(1).strip()
        add(i, name, created=meta.get("created", ""))
    return versions


def _to_record(path: Path, meta: dict, body: str) -> dict:
    stem, sections = _split_sections(body)
    solution = ""
    note = ""
    extra = []
    for heading, content in sections:
        if heading == "解析" and not solution:
            solution = content
        else:
            if heading == "备注" and not note:
                note = content
            extra.append((heading, content))
    rel = path.relative_to(config.BANK_DIR)
    folder = str(PurePosixPath(rel.parent.as_posix())) if rel.parent != Path(".") else ""
    qid = str(meta.get("id") or path.stem)
    rec = {
        "id": qid,
        # 文件名是题卡名称唯一真源。frontmatter 的 title 可能是用户在 Obsidian
        # 自定义的字段，不能占用；外部改文件名后界面也应立即跟着变化。
        "title": path.stem,
        "name": path.stem,
        "path": str(rel.as_posix()),
        "folder": folder,
        "body": stem,
        "solution": solution,
        "note": note,
        "extra_sections": extra,
        "type": meta.get("type", ""),
        "difficulty": str(meta.get("difficulty", "") or ""),
        "source": meta.get("source", ""),
        "tags": list(meta.get("tags", []) or []),
        "starred": bool(meta.get("starred", False)),
        "order": float(meta.get("order", 0.0) or 0.0),
        "img_align": str(meta.get("img_align", "") or ""),
        "img_width": (int(meta["img_width"]) if meta.get("img_width") not in (None, "") else None),
        # **空值必须留成 None，不能压成 ""**：`exporter.resolve_split` /
        # `plan_figs` 靠 `is None` 区分「用户从未设过」（给默认值：带图选择题整题
        # 分栏、四图选择题自动配对）与「明确关掉」（存 "off"，归 None 但不给默认）。
        # 压成 "" 就把这两种情形合并了，默认值永不生效，同一道题在两版里排版不同。
        "img_split": (str(meta["img_split"])
                      if meta.get("img_split") not in (None, "") else None),
        "img_layouts": list(meta.get("img_layouts", []) or []),
        "sol_img_split": (str(meta["sol_img_split"])
                          if meta.get("sol_img_split") not in (None, "") else None),
        "sol_img_layouts": list(meta.get("sol_img_layouts", []) or []),
        "img_originals": list(meta.get("img_originals", []) or []),
        "img_versions": _merge_img_versions(meta, body),
        "number": _as_number(meta.get("number")),
        "created": meta.get("created", ""),
        "updated": meta.get("updated", ""),
        # selected 刻意**不在这里算**：它不是文件的属性，而是纯内存态
        # （`_selected`），而本函数的产物会被 `_scan` 按 mtime 缓存住。勾选不改文件、
        # mtime 不变 → 缓存永不失效 → 这个字段会永远停在扫描那一刻的值。
        # 由 `_scan` 在每次返回前统一盖上（见那里的注释）。
    }
    return rec


def _skip_rel(rel: Path) -> bool:
    """该相对路径是否应被排除在题库之外。

    题库根可以直接是一个 Obsidian vault，所以除了自己的 .trash/_assets，
    还要跳过任何以点开头的目录（.obsidian 及各类插件的数据目录）。
    """
    for part in rel.parts[:-1] if rel.suffix else rel.parts:
        if part.startswith(".") or part in _RESERVED_BANK_DIRS:
            return True
    return False


def _question_record_from_path(
        path: Path, stat: os.stat_result | None = None) -> dict | None:
    """按路径读取一份题卡记录，并统一处理缓存与文档身份过滤。

    所有会把 Markdown 暴露给题库的入口都必须经过这里。否则全量扫描排除了普通
    文档，局部扫描或无限滚动却仍可能把同一文件当成题卡。
    """
    key = str(path)
    if stat is None:
        try:
            stat = path.stat()
        except OSError:
            return None
    cached = _cache.get(key)
    if (cached is not None
            and cached.get("_mtime_ns") == stat.st_mtime_ns
            and cached.get("_size") == stat.st_size):
        qid = str(cached.get("id") or path.stem)
        if _is_question_meta(cached.get("_meta", {})):
            _remember_question_identity(path, stat, qid, True)
            return cached
        _cache.pop(key, None)
        _remember_question_identity(path, stat, None, False)
        return None
    try:
        meta, body = _read_raw(path)
    except Exception:
        _cache.pop(key, None)
        _remember_question_identity(path, stat, None, False)
        return None
    is_question = _is_question_meta(meta)
    qid = (str(meta.get("id") or path.stem)
           if isinstance(meta, Mapping) else None)
    _remember_question_identity(path, stat, qid if is_question else None,
                                is_question)
    if not is_question:
        _cache.pop(key, None)
        return None
    rec = _to_record(path, meta, body)
    rec["_mtime"] = stat.st_mtime
    rec["_mtime_ns"] = stat.st_mtime_ns
    rec["_size"] = stat.st_size
    rec["_meta"] = meta
    _cache[key] = rec
    return rec


def _scan() -> dict[str, dict]:
    """扫描 BANK_DIR 下题卡 Markdown，跳过保留目录与显式文档类型。"""
    global _scan_snapshot, _scan_snapshot_root, _scan_snapshot_at
    root = config.BANK_DIR.resolve()
    now = time.monotonic()
    with _scan_lock:
        if (_scan_snapshot is not None and _scan_snapshot_root == root
                and now - _scan_snapshot_at < _SCAN_CACHE_SECONDS):
            found = _scan_snapshot
        else:
            found: dict[str, dict] = {}
            for path in config.BANK_DIR.rglob("*.md"):
                try:
                    rel = path.relative_to(config.BANK_DIR)
                except ValueError:
                    continue
                if _skip_rel(rel):
                    continue
                key = str(path)
                try:
                    stat = path.stat()
                except OSError:
                    continue
                rec = _question_record_from_path(path, stat)
                if rec is None:
                    continue
                found[key] = rec
            # 清掉已不存在的文件的缓存
            stale = set(_cache) - set(found)
            for k in stale:
                _cache.pop(k, None)
            _scan_snapshot = found
            _scan_snapshot_root = root
            _scan_snapshot_at = time.monotonic()
    # 勾选态每次现算，覆盖缓存里的旧值。
    #
    # 必须在这里做，不能放 `_to_record`：勾选是内存态，不改文件，mtime 不变，于是
    # 上面那条 `cached["_mtime"] == mtime` 分支会原样返回缓存记录——`selected`
    # 就永远停在该文件**首次被扫到**的那一刻。症状是 `count_selected()`（读
    # `_selected`）显示「已选 3 题」，而所有按记录过滤的地方都看不见这 3 题：
    # 导出/预览（scope=selected）报「没有可导出的题目」、「删除勾选」报「没有勾选
    # 任何题目」。缓存与内存态的生命周期不一样，就不能把后者算进被缓存的产物里。
    for rec in found.values():
        with _selected_lock:
            rec["selected"] = rec["id"] in _selected
    return found


def _all_records() -> list[dict]:
    return list(_scan().values())


def all_records_snapshot() -> list[dict]:
    """返回一次题库扫描的只读快照，供同一个页面请求复用。

    首页同时要题目、标签和文件夹计数。若三个入口各自调用 ``_scan``，题库变大后
    同一次请求会把全部 Markdown 的路径和 mtime 重扫数遍；调用方应只读这些记录，
    真正的写入仍走本模块现有的 CRUD 函数。
    """
    return _all_records()


def collection_records_snapshot(folder_id: str, *, recursive: bool = True) -> list[dict]:
    """只扫描一个文件夹，供局部浏览和写入排序避免遍历整座题库。

    默认包含后代目录，符合父文件夹汇总和筛选语义；计算新题 ``order`` 时只需要
    当前目录的直属题目，传 ``recursive=False`` 可避免一次导入误读整棵年份目录。
    """
    root = config.BANK_DIR.resolve()
    target = _folder_abspath(folder_id).resolve()
    if target != root and not target.is_relative_to(root):
        raise ValueError("文件夹路径越界")
    if not target.is_dir():
        return []
    records = []
    with _scan_lock:
        paths = target.rglob("*.md") if recursive else target.glob("*.md")
        for path in paths:
            try:
                rel = path.relative_to(config.BANK_DIR)
            except ValueError:
                continue
            if _skip_rel(rel):
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            record = _question_record_from_path(path, stat)
            if record is not None:
                records.append(record)
    with _selected_lock:
        selected = set(_selected)
    for rec in records:
        rec["selected"] = rec["id"] in selected
    return records


_NATURAL_PART_RE = re.compile(r"(\d+)")


def _natural_text_key(text: str) -> tuple:
    """按人眼习惯比较文本中的数字片段，例如卷2排在卷10前面。"""
    return tuple(
        (0, int(token)) if token.isdigit() else (1, token.casefold())
        for token in _NATURAL_PART_RE.split(str(text or "")) if token)


def _natural_rel_key(rel_path: str) -> tuple:
    """文件夹与题号按人眼顺序排列，避免第10题排在第2题前面。"""
    # 调用方传入的始终是 as_posix() 结果；这里直接拆字符串，避免三万条路径为了
    # 排序再各自构造一个 PurePosixPath 及其内部对象。
    return tuple(_natural_text_key(part) for part in rel_path.split("/"))


def list_question_paths(folder_id: str = "") -> list[str]:
    """只读 Markdown 头部列出题卡路径，供大题库无限滚动建立轻量快照。

    这里只快读 frontmatter 的 ``id`` 与 ``quizforge_kind``，复杂 YAML 才回退完整
    解析；题目正文仍等滚到对应批次时再读取。排序采用“文件夹/文件名自然序”；
    单卷普通视图仍走 frontmatter 的 ``order``，不会改变用户手工调整过的卷内顺序。
    """
    root = config.BANK_DIR.resolve()
    target = _folder_abspath(folder_id).resolve()
    if target != root and not target.is_relative_to(root):
        raise ValueError("文件夹路径越界")
    if not target.is_dir():
        return []
    snapshot_key = (str(root), str(target))
    now = time.monotonic()
    with _identity_cache_lock:
        snapshot = _question_paths_snapshot.get(snapshot_key)
        if (snapshot is not None
                and now - snapshot[0] < _QUESTION_PATHS_CACHE_SECONDS):
            return list(snapshot[1])
        generation = _question_paths_generation

    jobs = []
    # target 已经通过祖先关系校验，rglob 只会返回 bank_root 下的路径。字符串切片
    # 可避免为三万份文件重复构造 PurePosixPath/relative_to 对象；records_from_paths
    # 等外部传入路径的入口仍保留严格的 Path 校验。
    bank_root_text = root.as_posix().rstrip("/")
    bank_prefix_len = len(bank_root_text) + 1
    reserved = _RESERVED_BANK_DIRS
    for path in target.rglob("*.md"):
        path_text = path.as_posix()
        if not path_text.startswith(bank_root_text + "/"):
            continue
        rel_text = path_text[bank_prefix_len:]
        parts = rel_text.split("/")
        if any(part.startswith(".") or part in reserved
               for part in parts[:-1]):
            continue
        jobs.append((path, rel_text))

    paths = []
    if jobs:
        workers = min(_IDENTITY_SCAN_WORKERS, len(jobs))
        uncertain = []
        # stat 与头部快读都在工作线程执行。并发阶段不调用共享 ruamel 解析器；复杂
        # YAML 收集后串行回退完整解析。按线程数分批只创建约 32 个 Future，避免为
        # 三万份文件逐一提交任务的纯调度成本；轮转切片让各批磁盘位置分布更均匀。
        batches = [jobs[index::workers] for index in range(workers)]
        with ThreadPoolExecutor(max_workers=workers,
                                thread_name_prefix="question-kind") as executor:
            for batch_result in executor.map(
                    _question_path_identity_batch, batches):
                for rel_text, is_question, pending in batch_result:
                    if pending is not None:
                        uncertain.append((rel_text, pending))
                    elif is_question:
                        paths.append(rel_text)
        for rel_text, pending in uncertain:
            path, stat, peeked, raw_text = pending
            _qid, is_question = _resolve_question_identity(
                path, stat, peeked=peeked, raw_text=raw_text)
            if is_question:
                paths.append(rel_text)
    result = tuple(sorted(paths, key=_natural_rel_key))
    with _identity_cache_lock:
        if generation == _question_paths_generation:
            _question_paths_snapshot[snapshot_key] = (
                time.monotonic(), result)
    return list(result)


def records_from_paths(paths: list[str]) -> list[dict]:
    """读取指定的一小批相对路径，并复用现有 mtime 解析缓存。"""
    root = config.BANK_DIR.resolve()
    records = []
    with _scan_lock:
        for rel_text in paths:
            rel = PurePosixPath(rel_text)
            path = (config.BANK_DIR / rel).resolve()
            if (path == root or not path.is_relative_to(root)
                    or path.suffix.lower() != ".md"):
                continue
            try:
                disk_rel = path.relative_to(config.BANK_DIR)
            except ValueError:
                continue
            if _skip_rel(disk_rel):
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            record = _question_record_from_path(path, stat)
            if record is not None:
                records.append(record)
    refresh_selected(records)
    return records


_FRONTMATTER_PEEK_CHARS = 128 * 1024


_YAML_NULL_SCALARS = frozenset({"null", "Null", "NULL", "~", "''", '""'})
_YAML_COMPLEX_SCALAR_PREFIXES = frozenset("!&*|>@`[]{}%?,")


def _peek_simple_yaml_scalar(line: str, key: str) -> tuple[bool, str | None, bool]:
    """从一行顶层 YAML 读取简单标量，返回（是否该字段、值、是否确定）。"""
    prefix = f"{key}:"
    if not line.startswith(prefix):
        return False, None, True
    raw = line[len(prefix):].strip()
    if not raw or raw in _YAML_NULL_SCALARS:
        return True, None, True
    if raw[0] in {"'", '"'}:
        quote = raw[0]
        if len(raw) < 2 or raw[-1] != quote:
            return True, None, False
        value = raw[1:-1]
        # 转义引号需要真正的 YAML 解析器才能还原。
        if (quote == '"' and "\\" in value) or (quote == "'" and "''" in value):
            return True, None, False
        return True, value, True
    comment = re.search(r"\s+#", raw)
    value = raw[:comment.start()].rstrip() if comment else raw
    if not value or value[0] in _YAML_COMPLEX_SCALAR_PREFIXES:
        return True, None, False
    if any(char.isspace() for char in value):
        return True, None, False
    return True, value, True


def _peek_question_identity(path: Path, handle=None) -> tuple[str | None, bool, bool]:
    """只读 frontmatter 头部取得题目 id 与身份，不解析正文和完整 YAML。

    返回 ``(id, is_question, certain)``。复杂 YAML 标量交由调用方回退完整解析；
    没有身份字段的旧文件继续按题卡处理。
    """
    if handle is None:
        try:
            with path.open("r", encoding="utf-8", newline="") as opened:
                return _peek_question_identity(path, opened)
        except (OSError, UnicodeError):
            # 完整扫描同样会跳过读不出的文件，不应让一个坏文件拖慢整个选题篮。
            return None, False, True
    first = handle.readline()
    if first.rstrip("\r\n") != "---":
        return path.stem, True, True
    consumed = len(first)
    found_id = None
    id_seen = False
    kind_seen = False
    kind_value = None
    for line in handle:
        consumed += len(line)
        if consumed > _FRONTMATTER_PEEK_CHARS:
            return None, False, False
        stripped = line.rstrip("\r\n")
        if stripped == "---":
            is_question = (not kind_seen or _is_question_meta(
                {"quizforge_kind": kind_value}))
            return (found_id or path.stem), is_question, True
        matched, value, certain = _peek_simple_yaml_scalar(stripped, "id")
        if matched:
            if id_seen or not certain:
                return None, False, False
            id_seen = True
            found_id = value or path.stem
            continue
        matched, value, certain = _peek_simple_yaml_scalar(
            stripped, "quizforge_kind")
        if matched:
            if kind_seen or not certain:
                return None, False, False
            kind_seen = True
            kind_value = value
    # 缺少结束分隔线时 _parse_raw_text 也会按无 frontmatter 处理。
    return path.stem, True, True


def _read_question_identity_once(
        path: Path,
) -> tuple[os.stat_result, tuple[str | None, bool, bool], str | None]:
    """单次打开文件，以 fstat 建指纹并快读；复杂 YAML 顺便带回全文。"""
    with path.open("r", encoding="utf-8", newline="") as handle:
        stat = os.fstat(handle.fileno())
        peeked = _peek_question_identity(path, handle)
        raw_text = None
        if not peeked[2]:
            handle.seek(0)
            raw_text = handle.read()
    return stat, peeked, raw_text


def _question_path_identity_job(
        job: tuple[Path, str],
) -> tuple[str, bool, tuple | None]:
    """并发完成 stat/缓存判定；未知文件只 open 一次并用 fstat 建指纹。"""
    path, rel_text = job
    if _has_question_identity_cache(path):
        try:
            stat = path.stat()
        except OSError:
            return rel_text, False, None
        cached = _cached_question_identity(path, stat)
        if cached is not None:
            return rel_text, cached[1], None
    try:
        stat, peeked, raw_text = _read_question_identity_once(path)
    except Exception:
        return rel_text, False, None
    qid, is_question, certain = peeked
    if certain:
        _remember_question_identity(
            path, stat, qid if is_question else None, is_question)
        return rel_text, is_question, None
    return rel_text, False, (path, stat, peeked, raw_text)


def _question_path_identity_batch(jobs: list[tuple[Path, str]]) -> list[tuple]:
    """在线程内顺序处理一批路径，减少大题库的 Future 数量。"""
    return [_question_path_identity_job(job) for job in jobs]


def _resolve_question_identity(
        path: Path, stat: os.stat_result | None = None, *,
        peeked: tuple[str | None, bool, bool] | None = None,
        raw_text: str | None = None,
) -> tuple[str | None, bool]:
    """返回最终题卡身份；缓存命中不打开文件，复杂 YAML 才完整解析。"""
    if stat is None:
        if _has_question_identity_cache(path):
            try:
                stat = path.stat()
            except OSError:
                return None, False
            cached = _cached_question_identity(path, stat)
            if cached is not None:
                return cached
        try:
            stat, peeked, raw_text = _read_question_identity_once(path)
        except Exception:
            return None, False
    else:
        cached = _cached_question_identity(path, stat)
        if cached is not None:
            return cached
    qid, is_question, certain = peeked or (None, False, False)
    if not certain:
        try:
            meta, _body = (_parse_raw_text(raw_text) if raw_text is not None
                           else _read_raw(path))
        except Exception:
            qid, is_question = None, False
        else:
            is_question = _is_question_meta(meta)
            qid = (str(meta.get("id") or path.stem)
                   if is_question and isinstance(meta, Mapping) else None)
    _remember_question_identity(
        path, stat, qid if is_question else None, is_question)
    return qid, is_question


def _is_question_path(path: Path) -> bool:
    """按文件指纹复用身份；未命中时快读头部，复杂 YAML 再完整解析。"""
    _qid, is_question = _resolve_question_identity(path)
    return is_question


def records_from_ids(ids: list[str]) -> list[dict]:
    """按少量题目 id 读取记录，冷启动不解析整座题库。

    已展示题目优先命中 ``_cache``；其余只扫描 Markdown 的 frontmatter 头部来找
    路径，再复用 ``records_from_paths`` 完整解析命中的文件。复杂旧 frontmatter
    只回退解析当前文件，不为少量 id 扫描并解析整座题库。
    """
    ordered = list(dict.fromkeys(str(qid) for qid in ids if qid))
    if not ordered:
        return []
    wanted = set(ordered)
    cached_paths = {}
    with _scan_lock:
        for key, record in _cache.items():
            if not _is_question_meta(record.get("_meta", {})):
                continue
            qid = str(record.get("id") or "")
            if qid in wanted and Path(key).is_file():
                cached_paths[qid] = Path(key)

    records_by_id = {}
    cached_rel = []
    for qid in ordered:
        path = cached_paths.get(qid)
        if path is None:
            continue
        try:
            cached_rel.append(path.resolve().relative_to(
                config.BANK_DIR.resolve()).as_posix())
        except ValueError:
            continue
    for record in records_from_paths(cached_rel):
        qid = str(record.get("id") or "")
        if qid in wanted:
            records_by_id[qid] = record

    unresolved = wanted - set(records_by_id)
    found_paths = {}
    if unresolved:
        for path in config.BANK_DIR.rglob("*.md"):
            try:
                rel = path.relative_to(config.BANK_DIR)
            except ValueError:
                continue
            if _skip_rel(rel):
                continue
            qid, is_question = _resolve_question_identity(path)
            if is_question and qid in unresolved:
                found_paths[qid] = rel.as_posix()
                unresolved.remove(qid)
                if not unresolved:
                    break
        for record in records_from_paths([
                found_paths[qid] for qid in ordered if qid in found_paths]):
            qid = str(record.get("id") or "")
            if qid in wanted:
                records_by_id[qid] = record
    return [records_by_id[qid] for qid in ordered if qid in records_by_id]


def refresh_selected(records: list[dict]) -> None:
    """把长列表快照里的勾选态刷新为当前内存状态。"""
    with _selected_lock:
        selected = set(_selected)
    for rec in records:
        rec["selected"] = rec["id"] in selected


def _find_path_by_id(qid: str) -> Path | None:
    # 页面刚渲染过的题一定已在 mtime 缓存里。单题按钮若跳过这层直接 `_all_records()`，
    # 1.3 万题下每点一次分栏/难度/删除都会重新遍历整座 vault；写入又使全库快照
    # 失效，路由为了回渲再读一次时还会再扫一遍。先在内存记录里找，命中后只 stat
    # 这一份文件；缓存路径已被用户在 Obsidian 外部移动时再回落全库发现。
    needle = str(qid)
    with _scan_lock:
        for key, rec in _cache.items():
            if str(rec.get("id")) != needle:
                continue
            path = Path(key)
            if path.is_file():
                return path
    # 冷启动或 Obsidian 外部移动后只查看各文件的 frontmatter 头部；正常题卡的简单
    # id 无需把三万份正文和 YAML 全部解析。复杂旧 frontmatter 才由 records_from_ids
    # 内部回退完整扫描，以兼容历史题库。
    rows = records_from_ids([needle])
    if rows:
        return config.BANK_DIR / rows[0]["path"]
    return None


def get_question(qid: str) -> dict | None:
    """按 id 读取一题；已在页面出现过的题只重读这一份 Markdown。

    旧写法不仅调用 `_find_path_by_id` 两次，拿到路径后还用 `_scan()` 再查字典；只要
    前一步写盘使全库快照失效，这个“取一题”就会变成第二次全库扫描。这里沿用
    `records_from_paths` 的越界校验、mtime 缓存与勾选态刷新，避免另写一套读取规则。
    """
    path = _find_path_by_id(str(qid))
    if path is None:
        return None
    try:
        rel = path.resolve().relative_to(config.BANK_DIR.resolve()).as_posix()
    except ValueError:
        return None
    rows = records_from_paths([rel])
    return next((row for row in rows if str(row["id"]) == str(qid)), None)


# ---------------------------------------------------------------------------
# 文件夹（= 真实目录）
# ---------------------------------------------------------------------------


def _folder_abspath(folder_id: str) -> Path:
    """folder_id 是相对 BANK_DIR 的 posix 路径，'' 表示根目录。"""
    if not folder_id:
        return config.BANK_DIR
    return config.BANK_DIR / PurePosixPath(folder_id)


def _checked_folder_path(folder_id: str, *, allow_root: bool) -> Path:
    """把文件夹 id 收敛到题库内的非保留目录，拒绝越界和符号链接逃逸。"""
    folder_id = str(folder_id or "").strip("/")
    root = config.BANK_DIR.resolve()
    if not folder_id:
        if allow_root:
            return root
        raise ValueError("不能移动题库根目录")
    parts = PurePosixPath(folder_id).parts
    if (any(part in ("", ".", "..") or part.startswith(".")
            or part in _RESERVED_BANK_DIRS for part in parts)):
        raise ValueError("文件夹路径无效")
    path = _folder_abspath(folder_id).resolve()
    if path == root or not path.is_relative_to(root):
        raise ValueError("文件夹路径越界")
    return path


def list_collections_tree(records: list[dict] | None = None) -> list[dict]:
    """返回文件夹树：[{id,name,parent_id,cnt,depth,children:[...]}]，按名称字母序。"""
    global _tree_snapshot, _tree_snapshot_root, _tree_snapshot_at
    root = config.BANK_DIR.resolve()
    now = time.monotonic()
    # 只有显式传入空记录时，调用方才是在要“纯目录树”（导入目标/移动下拉框）。
    # 这棵树可以按目录结构缓存；带题数的树取决于传入 records，不能与它共用同一份
    # 快照，否则先开导入页会把 cnt=0 的树错误地喂给后续计数调用。
    directory_only = records is not None and not records
    with _scan_lock:
        if (directory_only and _tree_snapshot is not None
                and _tree_snapshot_root == root
                and now - _tree_snapshot_at < _SIDEBAR_CACHE_SECONDS):
            return _tree_snapshot
        counts: dict[str, int] = {}
        for rec in records if records is not None else _all_records():
            counts[rec["folder"]] = counts.get(rec["folder"], 0) + 1

        def build(dir_path: Path, parent_id: str, depth: int) -> list[dict]:
            try:
                subdirs = sorted(
                    # 与 _skip_rel 保持一致：点目录（.obsidian 等）不是题库文件夹
                    (p for p in dir_path.iterdir()
                     if p.is_dir() and not p.name.startswith(".")
                     and p.name not in _RESERVED_BANK_DIRS), key=lambda p: p.name)
            except FileNotFoundError:
                return []
            nodes = []
            for directory in subdirs:
                rel = directory.relative_to(config.BANK_DIR).as_posix()
                node = {
                    "id": rel,
                    "name": directory.name,
                    "parent_id": parent_id,
                    "cnt": counts.get(rel, 0),
                    "depth": depth,
                    "children": build(directory, rel, depth + 1),
                }
                node["cnt"] += sum(c["cnt"] for c in node["children"])
                nodes.append(node)
            return nodes

        tree = build(config.BANK_DIR, "", 0)
        if directory_only:
            _tree_snapshot = tree
            _tree_snapshot_root = root
            _tree_snapshot_at = time.monotonic()
        return tree


def _display_file_flags(*, display_pdf: bool = False,
                        display_md: bool = False,
                        show_pdf: bool | None = None,
                        show_md: bool | None = None,
                        show_cards: bool | None = None,
                        show_general_md: bool | None = None) -> tuple[bool, bool, bool]:
    """统一解析文件树展示开关。

    ``display_pdf``/``display_md`` 是早期调用方使用的参数，保留它们避免导入页和
    外部插件失效；新页面使用三个彼此独立的开关：题卡、PDF、一般 Markdown。
    旧的 ``display_md`` 只有在新开关完全未提供时才展开为两类 Markdown，兼容旧
    页面同时避免新页面把题卡误并入一般资料。
    """
    pdf = bool(display_pdf)
    if show_pdf is not None:
        pdf = bool(show_pdf)
    legacy_md = bool(display_md)
    if show_md is not None:
        legacy_md = bool(show_md)
    if show_cards is None and show_general_md is None:
        cards = legacy_md
        general = legacy_md
    else:
        cards = bool(show_cards)
        general = bool(show_general_md)
        # ``show_md`` 是当前旧 URL 的“显示 Markdown”开关，迁移到新协议时按
        # 一般资料解释；题卡必须显式勾选 show_cards，不能再次混入。
        if show_cards is None and show_md is not None:
            cards = False
        if show_general_md is None and show_md is not None:
            general = legacy_md
    return pdf, cards, general


def _display_markdown_identity(path: Path) -> tuple[bool, str | None]:
    """返回文件树展示用的 Markdown 身份。

    题库索引仍把无 frontmatter 的旧 Markdown 兼容为题卡，但文件树无法据此
    区分普通资料。展示层因此先确认 frontmatter；没有 frontmatter 的文件归入
    一般资料，带 frontmatter 的文件继续沿用题库的 kind 判定。
    """
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            stat = os.fstat(handle.fileno())
            first = handle.readline()
            if first.rstrip("\r\n") != "---":
                return False, None
            handle.seek(0)
            peeked = _peek_question_identity(path, handle)
            raw_text = None
            if not peeked[2]:
                handle.seek(0)
                raw_text = handle.read()
        qid, is_question = _resolve_question_identity(
            path, stat, peeked=peeked, raw_text=raw_text)
    except (OSError, UnicodeError, ValueError):
        return False, None
    return bool(is_question), (str(qid) if is_question and qid else None)


def list_display_files(folder_id: str = "", *, display_pdf: bool = False,
                       display_md: bool = False, show_pdf: bool | None = None,
                       show_md: bool | None = None,
                       show_cards: bool | None = None,
                       show_general_md: bool | None = None) -> list[dict]:
    """列出指定文件夹下需要显示在题库树中的直属文件。

    返回的 ``kind`` 保持 ``pdf``/``markdown`` 两种旧值；Markdown 另带
    ``markdown_kind``、``is_question`` 和 ``question_id``，让前端可以在同一文件
    树里区分题卡与一般资料，而不必再次猜测 frontmatter。
    """
    display_pdf, show_cards, show_general_md = _display_file_flags(
        display_pdf=display_pdf, display_md=display_md, show_pdf=show_pdf,
        show_md=show_md, show_cards=show_cards,
        show_general_md=show_general_md)
    if not (display_pdf or show_cards or show_general_md):
        return []
    root = _folder_abspath(folder_id).resolve()
    bank_root = config.BANK_DIR.resolve()
    if root != bank_root and not root.is_relative_to(bank_root):
        return []
    if not root.is_dir():
        return []
    result = []
    try:
        children = root.iterdir()
        for path in children:
            suffix = path.suffix.casefold()
            if (not path.is_file() or path.name.startswith(".")
                    or suffix == ".pdf" and not display_pdf
                    or suffix in _MARKDOWN_SUFFIXES
                    and not (show_cards or show_general_md)
                    or suffix not in {".pdf", *_MARKDOWN_SUFFIXES}):
                continue
            rel = path.relative_to(bank_root).as_posix()
            if _skip_rel(path.relative_to(bank_root)):
                continue
            is_question = False
            question_id = None
            if suffix in _MARKDOWN_SUFFIXES:
                is_question, question_id = _display_markdown_identity(path)
                if is_question and not show_cards:
                    continue
                if not is_question and not show_general_md:
                    continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            result.append({
                "name": path.name,
                "path": rel,
                "kind": "pdf" if suffix == ".pdf" else "markdown",
                "size": size,
                "is_question": is_question,
            })
            if suffix in _MARKDOWN_SUFFIXES:
                result[-1]["markdown_kind"] = (
                    "question" if is_question else "document")
                if question_id:
                    result[-1]["question_id"] = question_id
    except OSError:
        return []
    result.sort(key=lambda item: tuple(
        int(part) if part.isdigit() else part.casefold()
        for part in re.split(r"(\d+)", item["name"])))
    return result


def list_navigation_tree(active_id: str = "", *, display_pdf: bool = False,
                         display_md: bool = False, show_pdf: bool | None = None,
                         show_md: bool | None = None,
                         show_cards: bool | None = None,
                         show_general_md: bool | None = None) -> list[dict]:
    """返回侧栏所需的浅树，预载第一层和当前路径。

    完整树主要用于导入页和“移动到”选项；首页每次切换试卷都递归扫描 642 个
    目录没有必要。这里让顶层文件夹直接显示一级子目录，形成可读的层级树；更深层
    仍按需读取。深链接或刷新某个题集时继续预载当前路径，保证选中项可见。
    其余节点保留 ``has_children``，交给 ``/collections/children`` 点击后加载。
    """
    display_pdf, show_cards, show_general_md = _display_file_flags(
        display_pdf=display_pdf, display_md=display_md, show_pdf=show_pdf,
        show_md=show_md, show_cards=show_cards,
        show_general_md=show_general_md)
    root = config.BANK_DIR.resolve()

    def subdirs(path: Path) -> list[Path]:
        try:
            return sorted(
                (item for item in path.iterdir()
                 if item.is_dir() and not item.name.startswith(".")
                 and item.name not in _RESERVED_BANK_DIRS),
                key=lambda item: item.name)
        except OSError:
            return []

    def has_subdir(path: Path) -> bool:
        try:
            return any(
                item.is_dir() and not item.name.startswith(".")
                and item.name not in _RESERVED_BANK_DIRS for item in path.iterdir())
        except OSError:
            return False

    def has_display_file(path: Path) -> bool:
        if not (display_pdf or show_cards or show_general_md):
            return False
        try:
            return bool(list_display_files(
                path.relative_to(root).as_posix(), display_pdf=display_pdf,
                show_cards=show_cards, show_general_md=show_general_md))
        except OSError:
            return False

    def build(path: Path, parent_id: str, depth: int) -> list[dict]:
        nodes = []
        for directory in subdirs(path):
            rel = directory.relative_to(root).as_posix()
            # 只预载当前题集的祖先。当前节点自身即使还有子目录也保持折叠，
            # 更不能因为它位于顶层就自动展开整支树。
            # 当前节点自身保持折叠，只有祖先节点预展开；这样选中叶子题集时仍
            # 需要点击箭头才加载直属文件，同时不会牺牲“有文件即显示箭头”。
            on_active_path = active_id.startswith(rel + "/")
            expanded = on_active_path
            children = build(directory, rel, depth + 1) if expanded else []
            files = list_display_files(
                rel, display_pdf=display_pdf, show_cards=show_cards,
                show_general_md=show_general_md,
            ) if expanded else []
            nodes.append({
                "id": rel,
                "name": directory.name,
                "parent_id": parent_id,
                "cnt": 0,
                "depth": depth,
                "children": children,
                "files": files,
                "children_loaded": expanded,
                "has_children": (bool(children) or bool(files)) if expanded
                else (has_subdir(directory) or has_display_file(directory)),
            })
        return nodes

    return build(root, "", 0)


def all_collections(tree: list[dict] | None = None) -> list[dict]:
    """扁平化文件夹列表（供下拉框用），可复用已经建立的树。"""
    flat = []

    def walk(nodes):
        for n in nodes:
            flat.append(n)
            walk(n["children"])

    walk(tree if tree is not None else list_collections_tree())
    return flat


def list_collection_children(parent_id: str = "", *, display_pdf: bool = False,
                             display_md: bool = False, show_pdf: bool | None = None,
                             show_md: bool | None = None,
                             show_cards: bool | None = None,
                             show_general_md: bool | None = None) -> list[dict]:
    """只列一个目录的直接子文件夹，供前端展开树时按需读取。

    这里不统计题数、不扫描 Markdown，因此即使题库已有上万道题也只做一次很小的
    ``iterdir``。路径仍必须约束在 BANK_DIR 内，不能让只读接口变成目录探测器。
    """
    display_pdf, show_cards, show_general_md = _display_file_flags(
        display_pdf=display_pdf, display_md=display_md, show_pdf=show_pdf,
        show_md=show_md, show_cards=show_cards,
        show_general_md=show_general_md)
    root = config.BANK_DIR.resolve()
    parent = _folder_abspath(parent_id).resolve()
    if parent != root and not parent.is_relative_to(root):
        raise ValueError("文件夹路径越界")
    if not parent.is_dir():
        return []
    out = []
    for child in sorted(
            (path for path in parent.iterdir()
             if path.is_dir() and not path.name.startswith(".")
             and path.name not in _RESERVED_BANK_DIRS), key=lambda path: path.name):
        folder_id = child.relative_to(root).as_posix()
        files = list_display_files(
            folder_id, display_pdf=display_pdf, show_cards=show_cards,
            show_general_md=show_general_md)
        try:
            has_children = any(
                item.is_dir() and not item.name.startswith(".")
                and item.name not in _RESERVED_BANK_DIRS for item in child.iterdir())
        except OSError:
            has_children = False
        out.append({"id": folder_id, "name": child.name,
                    "has_children": has_children or bool(files),
                    "files": files})
    return out


def list_collection_files(folder_id: str = "", *, display_pdf: bool = False,
                          display_md: bool = False, show_pdf: bool | None = None,
                          show_md: bool | None = None,
                          show_cards: bool | None = None,
                          show_general_md: bool | None = None) -> list[dict]:
    """返回一个目录的直属展示文件（包括题库根目录）。

    ``list_collection_children`` 为兼容旧前端继续返回文件夹列表；需要把根目录文件
    交给新文件树时调用本函数即可，不必伪造一个“根文件夹”节点。
    """
    return list_display_files(
        folder_id, display_pdf=display_pdf, display_md=display_md,
        show_pdf=show_pdf, show_md=show_md, show_cards=show_cards,
        show_general_md=show_general_md)


def get_collection(folder_id: str,
                   collections: list[dict] | None = None) -> dict | None:
    if collections is None:
        # 这里只被用来验证目标目录存在并读取名字。旧实现为这件事先建立带题数的完整
        # 目录树，等价于解析全库 Markdown；批量移动第一步因此就要等几十秒。
        # 真实目录本身就是软件版题集的真相，直接验路径即可。
        folder_id = str(folder_id or "").strip("/")
        if not folder_id:
            return None
        root = config.BANK_DIR.resolve()
        path = _folder_abspath(folder_id).resolve()
        if (path == root or not path.is_relative_to(root) or not path.is_dir()
                or any(part.startswith(".") or part in _RESERVED_BANK_DIRS
                       for part in PurePosixPath(folder_id).parts)):
            return None
        parent_id = PurePosixPath(folder_id).parent.as_posix()
        if parent_id == ".":
            parent_id = ""
        return {
            "id": folder_id, "name": path.name, "parent_id": parent_id,
            "depth": len(PurePosixPath(folder_id).parts) - 1,
            "cnt": 0, "children": [],
        }
    for f in collections:
        if f["id"] == folder_id:
            return f
    return None


def get_folder_ancestors(folder_id: str) -> list[str]:
    if not folder_id:
        return []
    parts = PurePosixPath(folder_id).parts
    return [str(PurePosixPath(*parts[:i])) for i in range(1, len(parts) + 1)]


def create_collection(name: str, parent_id: str = "") -> str:
    parent_dir = _folder_abspath(parent_id)
    safe_name = name.strip() or "未命名文件夹"
    target = parent_dir / safe_name
    if target.exists():
        raise ValueError("同名文件夹已存在")
    target.mkdir(parents=True)
    invalidate_scan_cache(folder_structure=True)
    return str((PurePosixPath(parent_id) / safe_name)) if parent_id else safe_name


def safe_folder_name(name: str) -> str:
    """把任意字符串收成一个能当目录名的单段名字，收不出来返回空串。

    文件夹名这里会来自**上传文件名**（自动入库按文件名建组文件夹），路径分隔符和
    `..` 必须先掉——folder_id 是拼进 BANK_DIR 的相对路径，带一个 `../` 就能把题目
    写到题库外面去。Windows 保留字符一并换掉，否则 mkdir 直接抛。
    """
    name = (name or "").strip()
    for ch in '/\\:*?"<>|':
        name = name.replace(ch, "_")
    # 先换掉分隔符再消 `..`：顺序反了的话 `../..` 会剩下 `..`（strip(".") 只削
    # 首尾），拼进 folder_id 就能跳出题库目录。
    while ".." in name:
        name = name.replace("..", "_")
    return name.strip().strip(".").strip()


def get_or_create_collection(name: str, parent_id: str = "") -> str:
    """建文件夹，同名已存在就复用那一个，返回 folder_id。

    「打包为文件夹」和「不审核直接入库」都要这个语义：撞同名时报错中断整批导入
    是最差的结果——题已经切好摆在那了，为一个目录名把校对成果丢掉不值得。自动
    入库那条路更是没人在场回答「换个名字还是并进去」。
    """
    name = safe_folder_name(name)
    if not name:
        return ""
    target = _folder_abspath(parent_id) / name
    if target.is_dir():
        return str(PurePosixPath(parent_id) / name) if parent_id else name
    return create_collection(name, parent_id)


def rename_collection(folder_id: str, new_name: str) -> str:
    old = _folder_abspath(folder_id)
    new_name = new_name.strip()
    if not new_name:
        raise ValueError("名称不能为空")
    new_path = old.parent / new_name
    if new_path.exists():
        raise ValueError("同名文件夹已存在")
    old.rename(new_path)
    invalidate_scan_cache(folder_structure=True)
    new_id = str(new_path.relative_to(config.BANK_DIR).as_posix())
    return new_id


def move_folder(folder_id: str, new_parent_id: str) -> str:
    src = _checked_folder_path(folder_id, allow_root=False)
    dst_parent = _checked_folder_path(new_parent_id, allow_root=True)
    if not src.is_dir():
        raise ValueError("源文件夹不存在")
    if not dst_parent.is_dir():
        raise ValueError("目标父文件夹不存在")
    # 防止把文件夹移进自己或自己的子文件夹
    if dst_parent == src or dst_parent.is_relative_to(src):
        raise ValueError("不能移动到自己的子文件夹中")
    dst = dst_parent / src.name
    if dst.exists():
        raise ValueError("目标位置已存在同名文件夹")
    shutil.move(str(src), str(dst))
    invalidate_scan_cache(folder_structure=True)
    return str(dst.relative_to(config.BANK_DIR.resolve()).as_posix())


def delete_collection(folder_id: str):
    """整个子树软删：移入 .trash/，并记录原路径供恢复。"""
    src = _folder_abspath(folder_id)
    if not src.exists():
        return
    trash_name = f"{src.name}__{_new_id()}"
    dst = config.TRASH_DIR / trash_name
    shutil.move(str(src), str(dst))
    meta_path = dst / ".trash_meta.json"
    meta_path.write_text(
        json.dumps({"original_path": folder_id, "deleted_at": _now_iso()},
                   ensure_ascii=False),
        encoding="utf-8",
    )
    invalidate_scan_cache(folder_structure=True)


def list_deleted_collections() -> list[dict]:
    out = []
    if not config.TRASH_DIR.exists():
        return out
    for d in sorted(config.TRASH_DIR.iterdir()):
        if not d.is_dir():
            continue
        meta_path = d / ".trash_meta.json"
        if not meta_path.exists():
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        out.append({
            "id": d.name,
            "name": PurePosixPath(meta.get("original_path", d.name)).name,
            "original_path": meta.get("original_path", ""),
            "deleted_at": meta.get("deleted_at", ""),
        })
    return out


def restore_collection(trash_id: str):
    src = config.TRASH_DIR / trash_id
    meta_path = src / ".trash_meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    original = meta.get("original_path", "")
    dst = _folder_abspath(original) if original else config.BANK_DIR / src.name.rsplit("__", 1)[0]
    if dst.exists():
        raise FileExistsError("恢复目标位置已存在同名文件夹")
    meta_path.unlink()
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    invalidate_scan_cache(folder_structure=True)


def purge_collection(trash_id: str):
    target = config.TRASH_DIR / trash_id
    if target.exists():
        refs = _refs_under(target)
        shutil.rmtree(target)
        purge_orphan_images(refs)
        invalidate_scan_cache(folder_structure=True)


# ---------------------------------------------------------------------------
# 标签（无独立标签表，直接扫全库 frontmatter 的 tags 字段汇总）
# ---------------------------------------------------------------------------


def all_tags(records: list[dict] | None = None) -> list[str]:
    global _tags_snapshot, _tags_snapshot_root, _tags_snapshot_at
    root = config.BANK_DIR.resolve()
    now = time.monotonic()
    with _scan_lock:
        if (records is None and _tags_snapshot is not None
                and _tags_snapshot_root == root
                and now - _tags_snapshot_at < _SIDEBAR_CACHE_SECONDS):
            return _tags_snapshot
        source = records if records is not None else _all_records()
    seen: dict[str, int] = {}
    for rec in source:
        for t in rec["tags"]:
            seen[t] = seen.get(t, 0) + 1
    tags = sorted(seen, key=lambda t: (-seen[t], t))
    if records is not None:
        return tags
    with _scan_lock:
        _tags_snapshot = tags
        _tags_snapshot_root = root
        _tags_snapshot_at = time.monotonic()
    return tags


def tags_of(qid: str) -> list[str]:
    rec = get_question(qid)
    return rec["tags"] if rec else []


def rename_tag(old_name: str, new_name: str):
    old_name = old_name.strip()
    new_name = new_name.strip()
    if not new_name or old_name == new_name:
        return
    with _write_lock:
        for rec in _all_records():
            if old_name not in rec["tags"]:
                continue
            tags = [new_name if t == old_name else t for t in rec["tags"]]
            # 合并去重，保序
            deduped = list(dict.fromkeys(tags))
            _update_meta_fields(rec["id"], {"tags": deduped})


def add_tags_to(ids: list[str], tags: list[str]):
    tags = [t.strip() for t in tags if t.strip()]
    if not tags:
        return
    with _write_lock:
        for qid in ids:
            rec = get_question(qid)
            if not rec:
                continue
            merged = list(dict.fromkeys(rec["tags"] + tags))
            _update_meta_fields(qid, {"tags": merged})


# ---------------------------------------------------------------------------
# 题目 CRUD
# ---------------------------------------------------------------------------


def _top_order(folder: str) -> float:
    """文件夹里的下一个 `order`。**必须在 `_write_lock` 里调用**，见 create_question。"""
    orders = [
        record["order"]
        for record in collection_records_snapshot(folder, recursive=False)
    ]
    return (max(orders) + 1.0) if orders else 1.0


_WINDOWS_RESERVED_STEMS = frozenset({
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
})


def safe_question_title(title: str) -> str:
    """把题卡名称收敛成安全、可读的单段 Markdown 文件名（不含扩展名）。"""
    value = normalize_newlines(str(title or "")).replace("\n", " ").strip()
    if value.casefold().endswith(".md"):
        value = value[:-3].rstrip()
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value)
    while ".." in value:
        value = value.replace("..", "_")
    value = re.sub(r"\s+", " ", value).strip(" .")
    # Windows 按最后一个点前的主文件名判保留设备名，CON.md / CON.any 都不能创建。
    if value and value.split(".", 1)[0].upper() in _WINDOWS_RESERVED_STEMS:
        value = f"_{value}"
    # 给扩展名、冲突后缀和较深的题集路径留余量，避免组件名逼近 Windows 上限。
    return value[:180].rstrip(" .")


def default_question_title(source: str = "", number: int | None = None,
                           sequence: int | None = None, *,
                           temporary: bool = False) -> str:
    """按导入语义生成默认题卡名，不访问磁盘。

    ``number`` 是可靠原题号，优先于本批 ``sequence``；散题/快捷制卡由调用方传
    ``temporary=True``，生成“临时卡x”。没有题源也没有序号时返回空串，让旧入口
    继续以稳定 qid 命名。
    """
    number = _as_number(number)
    sequence = _as_number(sequence)
    ordinal = number if number is not None else sequence
    if temporary:
        return safe_question_title(
            f"临时卡{ordinal}" if ordinal is not None else "临时卡")
    safe_source = safe_question_title(source)
    if safe_source:
        return (safe_question_title(f"{safe_source}第{ordinal}题")
                if ordinal is not None else safe_source)
    return safe_question_title(f"第{number}题") if number is not None else ""


_TEMPORARY_TITLE_RE = re.compile(r"^临时卡(\d+)(?:_\d+)?$")


def _next_temporary_question_index(target_dir: Path) -> int:
    """在已持有 `_write_lock` 时计算下一个临时卡编号。"""
    maximum = 0
    if target_dir.is_dir():
        for path in target_dir.glob("*.md"):
            if not _is_question_path(path):
                continue
            match = _TEMPORARY_TITLE_RE.fullmatch(path.stem)
            if match:
                maximum = max(maximum, int(match.group(1)))
    return maximum + 1


def next_temporary_question_title(folder_id: str = "临时卡片") -> str:
    """返回指定题集内下一个“临时卡x”名称，仅供展示或预填。"""
    target_dir = _checked_folder_path(folder_id, allow_root=True)
    with _write_lock:
        index = _next_temporary_question_index(target_dir)
    return default_question_title(sequence=index, temporary=True)


def _question_filename(target_dir: Path, qid: str, number: int | None,
                       title: str = "", *, exclude: Path | None = None) -> Path:
    """返回题目 .md 的唯一落盘路径，重名时使用可读的 ``_2``、``_3`` 后缀。

    新题优先使用显式/生成后的 title；旧调用没有 title 时仍按“第x题 → qid”回退。
    文件名不参与身份认定，稳定身份始终来自 frontmatter 的 id。

    ``exclude`` 用于原文件就地改名：目标仍是本题当前路径时不视为冲突。
    """
    fallback = f"第{number}题" if number is not None else qid
    stem = safe_question_title(title) or safe_question_title(fallback) or qid
    excluded = exclude.resolve() if exclude is not None else None
    suffix = 1
    while True:
        candidate_stem = stem if suffix == 1 else f"{stem}_{suffix}"
        candidate = target_dir / f"{candidate_stem}.md"
        if excluded is not None and candidate.resolve() == excluded:
            return candidate
        if not candidate.exists():
            return candidate
        suffix += 1


def create_question(body: str, solution: str = "", qtype: str = "",
                     source: str = "", difficulty: str = "",
                     tags: list[str] | None = None, folder: str = "",
                     number: int | None = None, note: str = "",
                     title: str = "", *, temporary: bool = False) -> str:
    return create_questions_batch([{
        "body": body, "solution": solution, "type": qtype,
        "source": source, "difficulty": difficulty, "tags": tags or [],
        "number": number, "note": note, "title": title,
    }], folder, temporary=temporary)[0]


def create_questions_batch(items: list[dict], folder: str = "", *,
                           idempotency_scope: str = "",
                           temporary: bool = False) -> list[str]:
    """同一文件夹批量建题，只扫描一次现有 order。

    ``idempotency_scope`` 只供后台自动入库使用。作用域会和题目在本批中的下标一起
    生成稳定 qid，并写进 frontmatter；进程若在“题目已写入、任务状态尚未落盘”之间
    退出，重转会认回同一批题并补齐尚未写入的部分，而不是再造一套副本。普通手动
    导入不传该参数，仍保持每次提交都新建题目的既有语义。
    """
    if not items:
        return []
    target_dir = _folder_abspath(folder)
    created: list[str] = []
    scope = str(idempotency_scope or "").strip()
    written_paths: list[tuple[Path, str, int]] = []
    with _write_lock:
        # 取名、算序号、落盘必须在同一把锁里。批量导入若逐题调用
        # create_question，每道题的 _top_order 都会递归扫描整个 Obsidian 题库；
        # 历年卷达到数千题后，一卷 20 题就会重复扫描 20 次。这里一次取最大 order，
        # 后续在锁内递增，既保持并发安全，也把每卷全库扫描从 N 次降到 1 次。
        #
        # ① 取名：`_question_filename` 靠 `exists()` 判撞名，放到锁外的话两个并发
        #    导入（批量转换是多线程的）会同时看到「不存在」，后写的把先写的覆盖掉。
        # ② 序号：`_top_order` 读的是「已落盘的最大 order」，同样是读-改-写。
        #    2026-08-08 修的「导入丢顺序」就是这条——它原先写在上面 `meta.update`
        #    的字面量里、在锁外求值，`_convert_batch_worker` 用线程池并发跑各组、
        #    `_auto_import_after_convert` 又刻意在 `_batch_jobs_lock` 之外，于是
        #    落进同一文件夹的几组题会读到同一个 max，一批题拿到**相同的 order**。
        #    `_SORT_KEYS["custom"]` 只按 order 排，并列项的先后就交给 `rglob` 的
        #    返回顺序（文件系统决定，与题号无关）——表现正是「题目顺序乱掉」。
        next_order = _top_order(folder)
        next_temporary_index = (
            _next_temporary_question_index(target_dir) if temporary else None)
        # 稳定 qid 在调用前即可算出。按这些 id 定向读取能保留“题目被外部移动后仍不
        # 重复创建”的幂等语义，同时避免为了几十道题解析整座题库。
        expected_ids = [
            _stable_import_qid(scope, index) for index in range(len(items))
        ] if scope else []
        existing_by_id = {
            str(rec["id"]): rec for rec in records_from_ids(expected_ids)
        }
        try:
            for item_index, item in enumerate(items):
                qid = (_stable_import_qid(scope, item_index)
                       if scope else _new_id())
                now = _now_iso()
                source = str(item.get("source") or "")
                number = _as_number(item.get("number"))
                meta = dict(_KNOWN_DEFAULTS)
                meta.update({
                    "id": qid,
                    "type": str(item.get("type") or ""),
                    "source": source,
                    "difficulty": str(item.get("difficulty") or ""),
                    "tags": list(item.get("tags") or []),
                    "number": number,
                    "created": now,
                    "updated": now,
                    "order": next_order,
                })
                if scope:
                    # 两个字段必须随题持久化，不能只靠 qid 的哈希碰运气；若极小概率
                    # 撞到用户既有 qid，下面会因作用域不符而拒绝，绝不覆盖。
                    meta["_quizforge_import_scope"] = scope
                    meta["_quizforge_import_index"] = item_index
                # 导入链可直接给新题写入图片布局默认值。普通新增题不传这些键，仍沿用
                # _KNOWN_DEFAULTS；旧题也无需迁移。
                if item.get("img_split") in (
                        "opts", "full", "sub", "between", "after", "pair", "off"):
                    meta["img_split"] = item["img_split"]
                if isinstance(item.get("img_layouts"), list):
                    meta["img_layouts"] = list(item["img_layouts"])
                if item.get("sol_img_split") in ("full", "off"):
                    meta["sol_img_split"] = item["sol_img_split"]
                if isinstance(item.get("sol_img_layouts"), list):
                    meta["sol_img_layouts"] = list(item["sol_img_layouts"])
                note = str(item.get("note") or "").strip()
                extra = [("备注", note)] if note else []
                full_body = _join_sections(
                    str(item.get("body") or ""),
                    str(item.get("solution") or ""), extra)
                if scope:
                    # 只记录识别链拥有的字段；备注等额外分区属于用户内容，后续
                    # 增量刷新会原样保留，不把它们误判成识别结果。
                    meta[_IMPORT_BASELINE_DIGEST_KEY] = _import_owned_digest(
                        _import_item_record(item))

                existing = existing_by_id.get(qid)
                if existing is not None:
                    actual_meta = existing.get("_meta") or {}
                    expected_path = config.BANK_DIR / existing["path"]
                    expected = _to_record(expected_path, meta, full_body)
                    same_origin = (
                        actual_meta.get("_quizforge_import_scope") == scope
                        and _as_number(actual_meta.get(
                            "_quizforge_import_index")) == item_index)
                    same_content = all(
                        existing.get(key) == expected.get(key)
                        for key in ("body", "solution", "type", "source", "number",
                                    "img_split", "img_layouts", "sol_img_split",
                                    "sol_img_layouts"))
                    if not (same_origin and same_content):
                        raise ValueError(
                            "自动入库幂等标识与既有题目冲突，已停止以避免重复或覆盖")
                    created.append(qid)
                    continue

                explicit_title = safe_question_title(item.get("title") or "")
                if temporary and not explicit_title:
                    # 临时命名与题源元数据彼此独立。资料库截图仍要保留来源，但单题
                    # 制卡按产品规则命名为“临时卡x”；稳定 scope 重试认回既有题时
                    # 不消耗编号，因此进程在批次中途退出也不会因自己留下的部分题
                    # 造成后续编号漂移。
                    generated_title = default_question_title(
                        sequence=next_temporary_index, temporary=True)
                    next_temporary_index += 1
                else:
                    generated_title = default_question_title(
                        source, number,
                        item_index + 1 if source and number is None and len(items) > 1
                        else None)
                question_title = explicit_title or generated_title or qid

                path = _question_filename(
                    target_dir, qid, meta["number"], question_title)
                if path.exists():
                    # 正常随机 qid 几乎不会走到这里；稳定 qid 若对应文件损坏到无法
                    # 解析，也不能把它当成空位覆盖，交给用户保留现场处理。
                    raise FileExistsError(f"题目目标文件已存在：{path.name}")
                _write_raw(path, meta, full_body)
                written_paths.append((path, qid, item_index))
                created.append(qid)
                existing_by_id[qid] = _to_record(path, meta, full_body)
                existing_by_id[qid]["_meta"] = meta
                next_order += 1.0
        except Exception:
            # 批量接口对调用方保持“全有或全无”：只回滚本次调用刚写成、且仍带同一
            # 作用域/下标/qid 的文件。既有题、前次崩溃留下并已被认回的题都不碰。
            for path, qid, item_index in reversed(written_paths):
                try:
                    actual_meta, _body = _read_raw(path)
                    exact = (
                        str(actual_meta.get("id") or "") == qid
                        and actual_meta.get("_quizforge_import_scope") == scope
                        and _as_number(actual_meta.get(
                            "_quizforge_import_index")) == item_index)
                    if exact:
                        path.unlink()
                except (OSError, UnicodeError, ValueError):
                    pass
            if written_paths:
                invalidate_scan_cache()
            raise
    return created


def _stable_import_qid(scope: str, item_index: int) -> str:
    """后台幂等入库的稳定题目 id。创建与安全刷新必须共用同一口径。"""
    return hashlib.sha256(
        f"{scope}\0{item_index}".encode("utf-8")).hexdigest()[:12]


_IMPORT_OWNED_FIELDS = (
    "body", "solution", "type", "source", "number",
    "img_split", "img_layouts", "sol_img_split", "sol_img_layouts",
)

# 自动导入拥有的字段只占题卡元数据的一小部分。把它们的规范化摘要写进
# frontmatter，增量重转换即可识别“入库后用户改过正文/解析/题号”的题卡；标签、
# 难度、星标和其它自定义字段不在摘要内，因此这些用户元数据仍可安全保留。
_IMPORT_BASELINE_DIGEST_KEY = "_quizforge_import_baseline_digest"


def _import_owned_digest(row: Mapping) -> str:
    """返回自动导入字段的稳定摘要，供增量匹配的外部编辑检测使用。"""
    payload = {key: row.get(key) for key in _IMPORT_OWNED_FIELDS}
    # JSON 比 repr 更稳定：列表顺序有意义，ensure_ascii 让中文在不同默认编码下
    # 得到同一摘要；default=str 只作为旧/手写 frontmatter 异常值的保守兜底。
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _import_item_record(item: dict) -> dict:
    """把自动入库 payload 归一成可与现有题目逐项比较的字段。"""
    img_split = item.get("img_split")
    if img_split not in ("opts", "full", "sub", "between", "after",
                         "pair", "off"):
        img_split = None
    sol_img_split = item.get("sol_img_split")
    if sol_img_split not in ("full", "off"):
        sol_img_split = None
    return {
        "body": str(item.get("body") or ""),
        "solution": str(item.get("solution") or ""),
        "type": str(item.get("type") or ""),
        "source": str(item.get("source") or ""),
        "number": _as_number(item.get("number")),
        "img_split": img_split,
        "img_layouts": (list(item.get("img_layouts") or [])
                        if isinstance(item.get("img_layouts"), list) else []),
        "sol_img_split": sol_img_split,
        "sol_img_layouts": (list(item.get("sol_img_layouts") or [])
                            if isinstance(item.get("sol_img_layouts"), list)
                            else []),
    }


def refresh_questions_batch(items: list[dict], previous_items: list[dict],
                            folder: str = "", *,
                            idempotency_scope: str) -> list[str]:
    """安全刷新一组已自动入库的题，保留 qid、路径及用户元数据。

    只有库内每道题仍逐项等于 ``previous_items`` 时才允许写入；这证明用户没有在
    入库后修改识别链拥有的内容。数量、题号、作用域、下标、正文、解析、题型或
    布局任一变化都会在整组落盘前拒绝，绝不靠删题重建覆盖用户编辑。
    """
    scope = str(idempotency_scope or "").strip()
    if not scope:
        raise ValueError("安全刷新必须提供自动入库作用域")
    if not items or len(items) != len(previous_items):
        raise ValueError("安全刷新前后题目数量不一致")
    new_rows = [_import_item_record(item) for item in items]
    old_rows = [_import_item_record(item) for item in previous_items]
    if any(new["number"] != old["number"]
           for new, old in zip(new_rows, old_rows)):
        raise ValueError("安全刷新前后题号顺序不一致")

    target_dir = _folder_abspath(folder).resolve()
    expected_ids = [_stable_import_qid(scope, index)
                    for index in range(len(items))]
    staged: list[tuple[Path, Path, bytes]] = []
    replaced: list[tuple[Path, bytes]] = []
    with _write_lock:
        records = _all_records()
        by_id = {str(record["id"]): record for record in records}
        scoped_ids = {
            str(record["id"]) for record in records
            if (record.get("_meta") or {}).get(
                "_quizforge_import_scope") == scope
        }
        if scoped_ids != set(expected_ids):
            raise ValueError("已入库题目数量或身份发生变化，拒绝自动覆盖")

        prepared: list[tuple[Path, dict, str, bytes]] = []
        for index, (qid, old, new) in enumerate(
                zip(expected_ids, old_rows, new_rows)):
            record = by_id.get(qid)
            if record is None:
                raise ValueError("已入库题目缺失，拒绝自动覆盖")
            path = (config.BANK_DIR / record["path"]).resolve()
            if path.parent != target_dir or not path.is_file() or path.is_symlink():
                raise ValueError("已入库题目位置发生变化，拒绝自动覆盖")
            # 校验与后续回滚必须绑定同一份原始字节。若这里先 _read_raw、稍后再
            # read_bytes，Obsidian 恰好夹在两次读取之间保存，刷新会把新编辑误当
            # 成“旧基线”后覆盖。一次取字节、在内存解析可关闭这条窗口。
            original = path.read_bytes()
            try:
                meta, raw_body = _parse_raw_text(original.decode("utf-8"))
            except UnicodeDecodeError as exc:
                raise ValueError("已入库题目不是有效 UTF-8，拒绝自动覆盖") from exc
            actual = _to_record(path, meta, raw_body)
            same_origin = (
                str(meta.get("id") or "") == qid
                and meta.get("_quizforge_import_scope") == scope
                and _as_number(meta.get("_quizforge_import_index")) == index)
            same_content = all(actual.get(key) == old.get(key)
                               for key in _IMPORT_OWNED_FIELDS)
            if (not same_origin or not same_content
                    or actual.get("extra_sections")):
                raise ValueError(
                    "题目已在入库后被编辑，已停止刷新以避免覆盖用户修改")
            next_meta = dict(meta)
            next_meta.update({
                "type": new["type"], "source": new["source"],
                "number": new["number"], "img_split": new["img_split"],
                "img_layouts": new["img_layouts"],
                "sol_img_split": new["sol_img_split"],
                "sol_img_layouts": new["sol_img_layouts"],
                _IMPORT_BASELINE_DIGEST_KEY: _import_owned_digest(new),
                "updated": _now_iso(),
            })
            next_body = _join_sections(
                new["body"], new["solution"], [])
            prepared.append((path, next_meta, next_body, original))

        try:
            # 先把所有新内容完整序列化并写到同目录临时文件；只有全组均成功后才
            # 开始替换正式题卡。替换阶段若有磁盘错误，再用原始字节逐项回滚。
            for path, meta, body, original in prepared:
                if path.read_bytes() != original:
                    raise ValueError(
                        "题目在安全刷新期间被外部编辑，旧题已保留且未覆盖")
                temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
                temp.write_text(_render_raw(meta, body), encoding="utf-8",
                                newline="\n")
                staged.append((path, temp, original))
            for path, temp, original in staged:
                if path.read_bytes() != original:
                    raise ValueError(
                        "题目在安全刷新期间被外部编辑，旧题已保留且未覆盖")
                os.replace(temp, path)
                replaced.append((path, original))
        except Exception:
            for path, original in reversed(replaced):
                try:
                    path.write_bytes(original)
                except OSError:
                    logger.exception("安全刷新回滚失败：%s", path)
            raise
        finally:
            for _path, temp, _original in staged:
                temp.unlink(missing_ok=True)
            for path, _meta, _body, _original in prepared:
                _cache.pop(str(path), None)
            invalidate_scan_cache()
    return expected_ids


def refresh_questions_batch_incremental(
        items: list[dict], previous_items: list[dict] | None = None,
        folder: str = "", *, idempotency_scope: str = "",
        candidate_ids: list[str] | None = None,
        baseline: list[dict] | None = None) -> dict:
    """按题号/正文指纹把新识别结果增量合并到一个题集。

    这是给“原卷重新转换”使用的独立路径，故意不改变
    :func:`refresh_questions_batch` 的严格等长语义。题号唯一时优先按题号匹配；
    题号缺失、重复或找不到时再尝试唯一正文指纹。任何一对多、多对一、题号与
    指纹指向不同题卡，或检测到用户改动，都会返回 ``conflicts`` 并且整批不写盘。
    没有冲突时，匹配题只更新识别链拥有的字段，未匹配旧题保留，未匹配新题追加。

    ``previous_items`` 是可选的批次基线快照，元素可以是完整题库记录（含 ``id``）
    或仅含导入字段的旧 payload；完整记录能可靠检测用户在重转换期间的编辑。
    ``baseline`` 是同一快照的显式别名，便于恢复旧任务时调用。若两者都省略，函数
    只信任题卡 frontmatter 中由自动导入写入的基线摘要；没有摘要的旧题在内容变化
    时会进入审核而不会被覆盖。

    返回值始终是可序列化的字典：``updated``、``added``、``preserved`` 是 qid
    列表，``conflicts`` 是带 ``reason``/``new_index``/``qids`` 的审核项，另外提供
    ``written`` 和各项计数。发现输入/路径等程序错误仍抛 ``ValueError``，不把环境
    故障伪装成“无冲突”。
    """
    if baseline is not None:
        if previous_items is not None and previous_items != baseline:
            raise ValueError("增量刷新同时收到不同的基线快照")
        previous_items = baseline
    if not items:
        return {
            "ok": True, "written": False, "updated": [], "added": [],
            "preserved": [], "conflicts": [], "matched": [],
            "updated_count": 0, "added_count": 0, "preserved_count": 0,
            "conflict_count": 0,
        }

    scope = str(idempotency_scope or "").strip()
    target_dir = _checked_folder_path(folder, allow_root=True)
    if not target_dir.is_dir():
        raise ValueError("目标题集不存在")
    # `_checked_folder_path` 收敛了路径；这里保留规范化 id，写入结果和日志不会因
    # 用户用反斜杠/首尾斜杠提交而出现两个不同的目录标识。
    target_folder = (target_dir.relative_to(config.BANK_DIR.resolve())
                     .as_posix() if target_dir != config.BANK_DIR.resolve()
                     else "")

    def _result(*, updated=(), added=(), preserved=(), conflicts=(),
                matched=(), written=False):
        updated = list(updated)
        added = list(added)
        preserved = list(preserved)
        conflicts = list(conflicts)
        matched = list(matched)
        return {
            "ok": not conflicts,
            "written": bool(written),
            "updated": updated,
            "added": added,
            "preserved": preserved,
            "conflicts": conflicts,
            "matched": matched,
            "updated_count": len(updated),
            "added_count": len(added),
            "preserved_count": len(preserved),
            "conflict_count": len(conflicts),
        }

    def _conflict(reason: str, *, new_index: int | None = None,
                  qids=(), message: str = "", **extra) -> dict:
        row = {
            "reason": reason,
            # `kind`/`code` 是前端和旧脚本常用的别名，全部保留以便任务快照升级
            # 时不需要猜某一版本的字段名。
            "kind": reason,
            "code": reason,
            "qids": list(dict.fromkeys(str(qid) for qid in qids if qid)),
        }
        if new_index is not None:
            row["new_index"] = int(new_index)
        if message:
            row["message"] = message
        row.update(extra)
        return row

    def _qid_from_snapshot(row: Mapping) -> str:
        return str(row.get("id") or row.get("qid") or "").strip()

    def _snapshot_digest(row: Mapping, fallback_row: Mapping | None = None):
        meta = row.get("_meta") if isinstance(row, Mapping) else None
        meta = meta if isinstance(meta, Mapping) else {}
        for source in (row, meta):
            for key in (_IMPORT_BASELINE_DIGEST_KEY,
                        "baseline_digest", "_baseline_digest"):
                value = source.get(key) if isinstance(source, Mapping) else None
                if value:
                    return str(value)
        if fallback_row is not None:
            return _import_owned_digest(fallback_row)
        # A plain payload supplied as the baseline is itself authoritative. A full
        # current record is handled separately so that a missing digest on a legacy
        # card does not silently bless a later edit.
        if all(key in row for key in _IMPORT_OWNED_FIELDS):
            return _import_owned_digest(_import_item_record(row))
        return None

    def _current_row(record: Mapping) -> dict:
        return _import_item_record(record)

    # 所有读取、匹配和临时写入都在同一把锁内完成。这样两个原卷重转换同时指向
    # 同一题集时，不会拿到相同的 order，也不会把另一批刚追加的题误当成空位。
    with _write_lock:
        records = collection_records_snapshot(target_folder, recursive=False)
        by_id = {str(record.get("id")): record for record in records
                 if record.get("id")}

        selected_ids: set[str] | None = None
        candidate_ids_limited = candidate_ids is not None
        has_scope_identity_snapshot = False
        snapshots_by_id = {}
        snapshots_by_scope_index = {}
        missing_snapshot_ids: set[str] = set()
        missing_snapshot_scope_indexes: set[tuple[str, int]] = set()
        if candidate_ids is not None:
            selected_ids = {str(qid) for qid in candidate_ids if qid}
        if previous_items is not None:
            # 优先用快照中的稳定 qid；旧快照只有 scope/index 时也能认回。没有任何
            # 可辨认身份的旧 payload 才退回“本级全部题卡”，并在匹配阶段以题号/指纹
            # 消歧，宁可产生审核冲突也不静默更新无关题目。
            snapshot_ids: set[str] = set()
            unresolved_snapshot = []
            for snapshot in previous_items:
                if not isinstance(snapshot, Mapping):
                    unresolved_snapshot.append(snapshot)
                    continue
                qid = _qid_from_snapshot(snapshot)
                if qid:
                    snapshot_ids.add(qid)
                    continue
                meta = snapshot.get("_meta")
                meta = meta if isinstance(meta, Mapping) else snapshot
                snap_scope = str(meta.get("_quizforge_import_scope") or "")
                snap_index = _as_number(
                    meta.get("_quizforge_import_index"))
                if snap_scope and snap_index is not None:
                    has_scope_identity_snapshot = True
                    for record in records:
                        rmeta = record.get("_meta") or {}
                        if (str(rmeta.get("_quizforge_import_scope") or "")
                                == snap_scope
                                and _as_number(rmeta.get(
                                    "_quizforge_import_index")) == snap_index):
                            snapshot_ids.add(str(record["id"]))
                            break
                else:
                    unresolved_snapshot.append(snapshot)
            if snapshot_ids:
                selected_ids = ((selected_ids & snapshot_ids)
                                if selected_ids is not None else snapshot_ids)
            elif (unresolved_snapshot or has_scope_identity_snapshot) \
                    and selected_ids is None:
                selected_ids = set(by_id)
            elif selected_ids is None:
                selected_ids = set()

        if selected_ids is None:
            candidate_records = records
        else:
            candidate_records = [record for record in records
                                 if str(record.get("id")) in selected_ids]

        # 作用域属于自动入库的幂等身份。重试时即使批次快照只保存了新增题的
        # scope/index，也要把这些题重新纳入候选；它们不能因不在旧基线而重复追加。
        if scope:
            seen_candidate_ids = {str(record.get("id"))
                                  for record in candidate_records}
            for record in records:
                meta = record.get("_meta") or {}
                if (str(meta.get("_quizforge_import_scope") or "") == scope
                        and str(record.get("id")) not in seen_candidate_ids):
                    candidate_records.append(record)
                    seen_candidate_ids.add(str(record.get("id")))

        # 为每个候选建立基线。优先顺序是题卡 frontmatter 摘要，其次是显式快照；
        # 旧题没有任何基线时标记为 None，后面只有“新旧导入字段完全相同”才允许
        # 通过，避免把用户早先改过的题当成可覆盖对象。
        # 记住快照宣称存在、但当前题集里找不到的身份。题卡可能被用户删除、
        # 移动到其它题集，或被外部程序改了 frontmatter id；这些情况都不能被
        # 当作“新题”静默追加，否则重转换会留下重复题或覆盖范围外的数据。
        if previous_items is not None:
            for snapshot in previous_items:
                if not isinstance(snapshot, Mapping):
                    continue
                qid = _qid_from_snapshot(snapshot)
                key = None
                if qid:
                    key = ("id", qid)
                else:
                    meta = snapshot.get("_meta")
                    meta = meta if isinstance(meta, Mapping) else snapshot
                    snap_scope = str(meta.get("_quizforge_import_scope") or "")
                    snap_index = _as_number(meta.get("_quizforge_import_index"))
                    if snap_scope and snap_index is not None:
                        key = (snap_scope, snap_index)
                if key and key[0] == "id":
                    snapshots_by_id[key[1]] = snapshot
                elif key:
                    snapshots_by_scope_index[key] = snapshot

            # 只在快照明确提供身份时检查“消失”。没有 id/scope/index 的旧 payload
            # 本来就无法证明对应的是哪道旧题，继续交给题号/指纹匹配并在歧义时审核。
            for qid in snapshots_by_id:
                if qid not in by_id:
                    missing_snapshot_ids.add(qid)
            for scope_index in snapshots_by_scope_index:
                if not any(
                        str((record.get("_meta") or {}).get(
                            "_quizforge_import_scope") or "")
                        == scope_index[0]
                        and _as_number((record.get("_meta") or {}).get(
                            "_quizforge_import_index")) == scope_index[1]
                        for record in records):
                    missing_snapshot_scope_indexes.add(scope_index)

        candidate_info = []
        for record in candidate_records:
            qid = str(record.get("id") or "")
            snapshot = snapshots_by_id.get(qid)
            if snapshot is None:
                meta = record.get("_meta") or {}
                key = (str(meta.get("_quizforge_import_scope") or ""),
                       _as_number(meta.get("_quizforge_import_index")))
                snapshot = snapshots_by_scope_index.get(key)
            current = _current_row(record)
            baseline_row = (_import_item_record(snapshot)
                            if isinstance(snapshot, Mapping)
                            and all(key in snapshot for key in _IMPORT_OWNED_FIELDS)
                            else None)
            digest = _snapshot_digest(record, baseline_row)
            # `_snapshot_digest(record, baseline_row)` intentionally derives a digest
            # from the explicit snapshot. For a legacy record with no snapshot/digest,
            # it returns None and edit detection remains conservative below.
            candidate_info.append({
                "record": record, "qid": qid, "current": current,
                "digest": digest, "baseline_row": baseline_row,
            })

        # 上述 helper 在“无 snapshot 但完整当前记录”时会把当前内容当 baseline；这
        # 只适合显式 previous_items 为空的旧调用吗？为了不覆盖 legacy 用户编辑，
        # 纠正为：只有 frontmatter 明确有摘要，或 snapshot 显式提供字段时才信任。
        for info in candidate_info:
            record = info["record"]
            meta = record.get("_meta") or {}
            explicit = any(meta.get(key)
                           for key in (_IMPORT_BASELINE_DIGEST_KEY,
                                       "baseline_digest", "_baseline_digest"))
            if not explicit and info["baseline_row"] is None:
                info["digest"] = None

        number_index: dict[int, list[dict]] = {}
        fingerprint_index: dict[str, list[dict]] = {}
        for info in candidate_info:
            number = info["current"].get("number")
            if number is not None:
                number_index.setdefault(number, []).append(info)
            fp = dedup.fingerprint(info["current"].get("body") or "")
            fingerprint_index.setdefault(fp, []).append(info)

        new_rows = [_import_item_record(item) for item in items]
        new_fps = [dedup.fingerprint(row.get("body") or "")
                   for row in new_rows]
        # 新结果自身出现同一正文时，无论题号是否不同都无法安全决定一对一归属。
        duplicate_new_fps: dict[str, list[int]] = {}
        for index, fp in enumerate(new_fps):
            duplicate_new_fps.setdefault(fp, []).append(index)
        duplicate_new_fps = {
            fp: indexes for fp, indexes in duplicate_new_fps.items()
            if len(indexes) > 1
        }

        conflicts: list[dict] = []
        if candidate_ids_limited:
            # 显式候选身份已经从磁盘消失时，同样不能把对应新结果当成
            # 无关新增；调用方通常是在恢复任务快照，丢失应进入人工审核。
            for qid in sorted((selected_ids or set()) - set(by_id)):
                if qid in missing_snapshot_ids:
                    continue
                conflicts.append(_conflict(
                    "candidate_missing", qids=(qid,),
                    message="指定的候选题卡已从当前题集消失，无法安全增量写入"))
        # candidate_ids 表示调用方只要求处理一个子集；未选中的快照缺失不应
        # 阻断这次局部刷新。未传 candidate_ids 时，所有显式身份都必须仍可找到。
        for qid in sorted(missing_snapshot_ids):
            if candidate_ids_limited and qid not in (selected_ids or set()):
                continue
            conflicts.append(_conflict(
                "baseline_missing", qids=(qid,),
                message="基线题卡已从当前题集消失，无法安全判断是删除还是移动"))
        for snap_scope, snap_index in sorted(missing_snapshot_scope_indexes):
            if candidate_ids_limited:
                # scope/index 快照没有 qid，只有在当前候选集合中存在同作用域题卡
                # 才能被 candidate_ids 选择；局部调用无法证明其缺失，跳过该项。
                continue
            conflicts.append(_conflict(
                "baseline_missing", new_index=snap_index,
                message="基线题卡已从当前题集消失，无法安全判断是删除还是移动",
                scope=snap_scope, index=snap_index))
        matches: list[dict] = []
        matched_by_new: dict[int, dict] = {}
        used_old: set[str] = set()
        blocked_new: set[int] = set()
        for fp, indexes in duplicate_new_fps.items():
            blocked_new.update(indexes)
            conflicts.append(_conflict(
                "duplicate_new_fingerprint", new_index=indexes[0],
                new_indexes=indexes, fingerprint=fp,
                message="新识别结果存在重复正文，无法一对一增量匹配"))

        for index, (new_row, new_fp) in enumerate(zip(new_rows, new_fps)):
            if index in blocked_new:
                continue
            number = new_row.get("number")
            number_candidates = (list(number_index.get(number, []))
                                 if number is not None else [])
            fp_candidates = list(fingerprint_index.get(new_fp, []))
            number_choice = (number_candidates[0]
                             if len(number_candidates) == 1 else None)
            fp_choice = (fp_candidates[0]
                         if len(fp_candidates) == 1 else None)
            chosen = None
            method = ""
            if number_choice is not None:
                if fp_choice is not None and fp_choice["qid"] != number_choice["qid"]:
                    conflicts.append(_conflict(
                        "number_fingerprint_conflict", new_index=index,
                        qids=(number_choice["qid"], fp_choice["qid"]),
                        number=number, fingerprint=new_fp,
                        message="题号和正文指纹指向不同题卡"))
                    continue
                chosen = number_choice
                method = "number"
            elif fp_choice is not None:
                # 题号重复/缺失/找不到时，唯一正文指纹是允许的兜底。
                if number_candidates and all(
                        fp_choice["qid"] != candidate["qid"]
                        for candidate in number_candidates):
                    conflicts.append(_conflict(
                        "number_fingerprint_conflict", new_index=index,
                        qids=[info["qid"] for info in number_candidates]
                        + [fp_choice["qid"]], number=number,
                        fingerprint=new_fp,
                        message="重复题号与正文指纹指向不同题卡"))
                    continue
                chosen = fp_choice
                method = "fingerprint"
            elif number_candidates:
                conflicts.append(_conflict(
                    "ambiguous_number", new_index=index,
                    qids=[info["qid"] for info in number_candidates],
                    number=number,
                    message="旧题号对应多道题，正文指纹也无法唯一消歧"))
                continue
            elif fp_candidates:
                conflicts.append(_conflict(
                    "ambiguous_fingerprint", new_index=index,
                    qids=[info["qid"] for info in fp_candidates],
                    fingerprint=new_fp,
                    message="正文指纹对应多道旧题，无法安全匹配"))
                continue
            else:
                # 没有旧题对应，稍后追加。
                continue

            if chosen["qid"] in used_old:
                conflicts.append(_conflict(
                    "many_to_one", new_index=index, qids=(chosen["qid"],),
                    message="多个新结果试图更新同一旧题"))
                continue
            used_old.add(chosen["qid"])
            matched_by_new[index] = chosen
            matches.append({"new_index": index, "qid": chosen["qid"],
                            "method": method})

        if conflicts:
            preserved = [info["qid"] for info in candidate_info]
            return _result(preserved=preserved, conflicts=conflicts,
                           matched=matches, written=False)

        # 生成更新/追加计划。先完成所有基线和路径校验，再开始写临时文件，保证
        # 用户遇到冲突时不会看到半批新题或半批旧题已经被替换。
        updated_infos = []
        additions = []
        preserved = []
        next_order = _top_order(target_folder)
        candidate_by_qid = {info["qid"]: info for info in candidate_info}
        for index, (item, new_row) in enumerate(zip(items, new_rows)):
            info = matched_by_new.get(index)
            if info is None:
                additions.append((index, item, new_row, new_fps[index]))
                continue
            current = info["current"]
            baseline_digest = info.get("digest")
            baseline_row = info.get("baseline_row")
            current_digest = _import_owned_digest(current)
            if baseline_digest:
                baseline_changed = current_digest != baseline_digest
            elif baseline_row is not None:
                baseline_changed = current != baseline_row
            else:
                # 没有可靠基线时，仅当新识别内容与当前完全一致才可继续；否则转
                # 人工审核。这样老版本手工题卡不会因“题号碰巧相同”被覆盖。
                baseline_changed = current != new_row
            if baseline_changed:
                conflicts.append(_conflict(
                    "user_edited", new_index=index, qids=(info["qid"],),
                    message="题卡在上次入库后被用户编辑，已保留原题并转人工审核"))
                continue
            updated_infos.append((index, info, new_row))

        if conflicts:
            preserved = [info["qid"] for info in candidate_info]
            return _result(preserved=preserved, conflicts=conflicts,
                           matched=matches, written=False)

        existing_ids = set(by_id)
        prepared: list[dict] = []
        # 更新项：复制整个 frontmatter，仅替换识别链拥有字段和新的基线摘要。
        for index, info, new_row in updated_infos:
            record = info["record"]
            # 必须在 resolve 前检查链接本身；对已解析的目标调用 is_symlink()
            # 永远是 False，会把题库内指向外部的链接误当成可覆盖的普通题卡。
            raw_path = config.BANK_DIR / PurePosixPath(
                str(record.get("path") or ""))
            if (_is_link_or_junction(raw_path)
                    or not raw_path.exists()):
                conflicts.append(_conflict(
                    "path_changed", new_index=index, qids=(info["qid"],),
                    message="旧题卡位置发生变化，已停止增量写入"))
                continue
            path = raw_path.resolve()
            if (path.parent != target_dir or not path.is_file()):
                conflicts.append(_conflict(
                    "path_changed", new_index=index, qids=(info["qid"],),
                    message="旧题卡位置发生变化，已停止增量写入"))
                continue
            original = path.read_bytes()
            try:
                meta, raw_body = _parse_raw_text(original.decode("utf-8"))
            except UnicodeDecodeError as exc:
                conflicts.append(_conflict(
                    "invalid_utf8", new_index=index, qids=(info["qid"],),
                    message="旧题卡不是有效 UTF-8，已停止增量写入"))
                continue
            actual = _to_record(path, meta, raw_body)
            if str(meta.get("id") or path.stem) != info["qid"]:
                conflicts.append(_conflict(
                    "identity_changed", new_index=index, qids=(info["qid"],),
                    message="旧题卡身份发生变化，已停止增量写入"))
                continue
            # 与最初匹配时使用的内容再次核对，覆盖“读取快照后 Obsidian 保存”的窗口。
            if _import_owned_digest(_current_row(actual)) != _import_owned_digest(info["current"]):
                conflicts.append(_conflict(
                    "external_edit", new_index=index, qids=(info["qid"],),
                    message="写入前检测到外部编辑，旧题已保留"))
                continue
            next_meta = dict(meta)
            next_meta.update({
                "id": info["qid"], "type": new_row["type"],
                "source": new_row["source"], "number": new_row["number"],
                "img_split": new_row["img_split"],
                "img_layouts": new_row["img_layouts"],
                "sol_img_split": new_row["sol_img_split"],
                "sol_img_layouts": new_row["sol_img_layouts"],
                _IMPORT_BASELINE_DIGEST_KEY: _import_owned_digest(new_row),
                "updated": _now_iso(),
            })
            if scope:
                next_meta["_quizforge_import_scope"] = scope
                next_meta["_quizforge_import_index"] = index
            extra = list(actual.get("extra_sections") or [])
            next_body = _join_sections(new_row["body"], new_row["solution"], extra)
            prepared.append({"path": path, "original": original,
                             "meta": next_meta, "body": next_body,
                             "qid": info["qid"], "new": False})

        if conflicts:
            preserved = [info["qid"] for info in candidate_info]
            return _result(preserved=preserved, conflicts=conflicts,
                           matched=matches, written=False)

        # 追加项的稳定 id 以题号/指纹构成，批次重试或题目插入后仍能认回同一道新题。
        for index, item, new_row, new_fp in additions:
            if scope:
                token = (f"number:{new_row['number']}"
                         if new_row.get("number") is not None
                         else f"fingerprint:{new_fp}")
                qid = _stable_import_qid(f"{scope}:incremental:{token}", 0)
            else:
                qid = _new_id()
            if qid in existing_ids:
                # 已有同一稳定 id 但未被上面的索引选中，不能覆盖/重复追加。
                conflicts.append(_conflict(
                    "stable_id_collision", new_index=index, qids=(qid,),
                    message="增量追加的稳定题目身份已被其它题卡占用"))
                continue
            source = str(new_row.get("source") or "")
            number = new_row.get("number")
            explicit_title = safe_question_title(item.get("title") or "")
            generated_title = default_question_title(
                source, number,
                index + 1 if source and number is None and len(items) > 1
                else None)
            title = explicit_title or generated_title or qid
            path = _question_filename(target_dir, qid, number, title)
            meta = dict(_KNOWN_DEFAULTS)
            meta.update({
                "id": qid, "type": new_row["type"],
                "source": new_row["source"], "number": number,
                # 增量重转换的新增题可能来自人工审核确认；这些是新题的初始
                # 用户元数据，应沿用表单值。匹配到的旧题则在上面整份保留，
                # 不会用识别结果覆盖既有标签/难度/星标。
                "tags": list(item.get("tags") or []),
                "difficulty": str(item.get("difficulty") or ""),
                "starred": bool(item.get("starred", False)),
                "created": _now_iso(), "updated": _now_iso(),
                "order": next_order,
                _IMPORT_BASELINE_DIGEST_KEY: _import_owned_digest(new_row),
            })
            if scope:
                meta["_quizforge_import_scope"] = scope
                meta["_quizforge_import_index"] = index
            if new_row.get("img_split") is not None:
                meta["img_split"] = new_row["img_split"]
            if isinstance(new_row.get("img_layouts"), list):
                meta["img_layouts"] = list(new_row["img_layouts"])
            if new_row.get("sol_img_split") is not None:
                meta["sol_img_split"] = new_row["sol_img_split"]
            if isinstance(new_row.get("sol_img_layouts"), list):
                meta["sol_img_layouts"] = list(new_row["sol_img_layouts"])
            note = str(item.get("note") or "").strip()
            extra = [("备注", note)] if note else []
            body = _join_sections(new_row["body"], new_row["solution"], extra)
            prepared.append({"path": path, "original": None, "meta": meta,
                             "body": body, "qid": qid, "new": True})
            existing_ids.add(qid)
            next_order += 1.0

        if conflicts:
            preserved = [info["qid"] for info in candidate_info]
            return _result(preserved=preserved, conflicts=conflicts,
                           matched=matches, written=False)

        staged: list[tuple[dict, Path]] = []
        replaced: list[tuple[dict, bytes]] = []
        try:
            for plan in prepared:
                path = plan["path"]
                if plan["new"]:
                    if path.exists() or path.is_symlink():
                        raise ValueError(f"增量追加目标已存在：{path.name}")
                elif path.read_bytes() != plan["original"]:
                    raise ValueError("题卡在增量刷新期间被外部编辑，旧题已保留")
                temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
                temp.write_text(_render_raw(plan["meta"], plan["body"]),
                                encoding="utf-8", newline="\n")
                staged.append((plan, temp))
            for plan, temp in staged:
                path = plan["path"]
                if plan["new"]:
                    if path.exists() or path.is_symlink():
                        raise ValueError(f"增量追加目标已存在：{path.name}")
                elif path.read_bytes() != plan["original"]:
                    raise ValueError("题卡在增量刷新期间被外部编辑，旧题已保留")
                os.replace(temp, path)
                replaced.append((plan, plan["original"]))
        except Exception:
            for plan, original in reversed(replaced):
                path = plan["path"]
                try:
                    if original is None:
                        path.unlink(missing_ok=True)
                    else:
                        path.write_bytes(original)
                except OSError:
                    logger.exception("增量刷新回滚失败：%s", path)
            raise
        finally:
            for _plan, temp in staged:
                temp.unlink(missing_ok=True)
            for plan, _temp in staged:
                _cache.pop(str(plan["path"]), None)
            if staged:
                invalidate_scan_cache(
                    folder_structure=any(plan["new"] for plan, _ in staged))

        updated = [info["qid"] for _index, info, _row in updated_infos]
        added = [plan["qid"] for plan in prepared if plan["new"]]
        matched_ids = set(updated) | set(added)
        preserved = [info["qid"] for info in candidate_info
                     if info["qid"] not in matched_ids]
        return _result(updated=updated, added=added, preserved=preserved,
                       matched=matches, written=bool(prepared))


def update_question(qid: str, body: str, solution: str = "", qtype: str = "",
                     source: str = "", difficulty: str = "",
                     tags: list[str] | None = None, note: str | None = None):
    rec = get_question(qid)
    if not rec:
        raise KeyError(qid)
    path = config.BANK_DIR / rec["path"]
    meta = dict(rec["_meta"])
    meta.update({
        "type": qtype,
        "source": source,
        "difficulty": difficulty,
        "tags": list(tags) if tags is not None else rec["tags"],
        "updated": _now_iso(),
    })
    extra = rec["extra_sections"]
    if note is not None:
        extra = _replace_note_section(extra, note)
    full_body = _join_sections(body, solution, extra)
    with _write_lock:
        _write_raw(path, meta, full_body)


def rename_question(qid: str, new_title: str) -> dict:
    """只重命名 Markdown 文件，正文、frontmatter、稳定 id 与图片均不改写。"""
    requested = safe_question_title(new_title)
    if not requested:
        raise ValueError("题卡名称不能为空")
    with _write_lock:
        rec = get_question(qid)
        if not rec:
            raise KeyError(qid)
        src = config.BANK_DIR / rec["path"]
        dst = _question_filename(
            src.parent, str(rec["id"]), rec.get("number"), requested,
            exclude=src)
        if dst.resolve() != src.resolve():
            # Windows 的同目录 rename 不覆盖既有目标；正文始终由文件系统直接移动，
            # 不存在“读出旧正文后覆盖 Obsidian 新保存内容”的窗口。
            src.rename(dst)
            _cache.pop(str(src), None)
            _cache.pop(str(dst), None)
            invalidate_scan_cache()
    renamed = get_question(qid)
    if renamed is None:  # 文件已成功写入但无法重新读取时保留明确故障，不伪造结果。
        raise OSError("题卡重命名后无法重新读取")
    return renamed


def _update_meta_fields(qid: str, fields: dict):
    """就地更新某题 frontmatter 里的若干字段，正文不变。"""
    with _write_lock:
        rec = get_question(qid)
        if not rec:
            return
        path = config.BANK_DIR / rec["path"]
        meta, body = _read_raw(path)
        meta.update(fields)
        meta["updated"] = _now_iso()
        _write_raw(path, meta, body)


def delete_question(qid: str) -> dict | None:
    rec = get_question(qid)
    if not rec:
        return None
    src = config.BANK_DIR / rec["path"]
    trash_name = f"{src.stem}__{_new_id()}{src.suffix}"
    dst = config.TRASH_DIR / trash_name
    meta, body = _read_raw(src)
    meta["_trash_original_path"] = rec["path"]
    meta["_trash_deleted_at"] = _now_iso()
    with _write_lock:
        _write_raw(src, meta, body)  # 先落盘记录原路径
        shutil.move(str(src), str(dst))
        invalidate_scan_cache()
    with _selected_lock:
        if qid in _selected:
            _selected.discard(qid)
            _save_selected_unlocked()
    return rec


def _checked_bank_file(path_or_rel: Path | str) -> tuple[Path, str]:
    """校验一个题库内的真实文件，返回绝对路径和 POSIX 相对路径。"""
    raw = path_or_rel
    if isinstance(raw, Path):
        candidate = raw
    else:
        value = str(raw or "").strip().replace("\\", "/")
        rel = PurePosixPath(value)
        if rel.is_absolute() or any(part in ("", ".", "..")
                                    or part.startswith(".")
                                    for part in rel.parts):
            raise ValueError("文件路径无效")
        candidate = config.BANK_DIR.joinpath(*rel.parts)
    try:
        # 先判断链接本身，再 resolve；否则指向题库外的链接会被误当成普通文件。
        if candidate.is_symlink() or getattr(candidate, "is_junction", lambda: False)():
            raise ValueError("不支持操作符号链接或目录联接")
        root = config.BANK_DIR.resolve()
        target = candidate.resolve()
        if target == root or not target.is_relative_to(root):
            raise ValueError("文件路径越界")
        rel = target.relative_to(root)
    except OSError as exc:
        raise ValueError("文件路径无效") from exc
    if (not target.is_file() or target.name.startswith(".")
            or _skip_rel(rel)):
        raise ValueError("文件不存在或不可操作")
    return target, rel.as_posix()


def trash_file(path_or_rel: Path | str, *, kind: str = "") -> dict:
    """把题库内普通文件移入回收站，并记录原路径。

    题卡 Markdown 应调用 ``delete_question``，以便沿用题目回收/恢复和图片引用
    语义；此函数用于 PDF、图片、Word 及显式 ``document`` Markdown。普通文件不能
    把原路径写进自身，因此在回收站旁放一个同名 ``.trash_meta.json`` 侧车。
    """
    source, rel = _checked_bank_file(path_or_rel)
    config.TRASH_DIR.mkdir(parents=True, exist_ok=True)
    suffix = source.suffix
    trash_name = f"{source.stem}__{_new_id()}{suffix}"
    destination = config.TRASH_DIR / trash_name
    metadata_path = destination.with_name(destination.name + ".trash_meta.json")
    metadata = {
        "original_path": rel,
        "deleted_at": _now_iso(),
        "kind": str(kind or source.suffix.lstrip(".")),
    }
    with _write_lock:
        # shutil.move 在同盘时仍可能覆盖已有目标；随机 id 已足够避免正常冲突，
        # 这里再做一次显式检查，防止极低概率的 mock/竞态把回收站文件替换掉。
        if destination.exists() or metadata_path.exists():
            raise FileExistsError("回收站目标已存在")
        shutil.move(str(source), str(destination))
        try:
            metadata_path.write_text(
                json.dumps(metadata, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8", newline="\n")
        except Exception:
            # 元数据写失败时恢复原文件，避免留下无法恢复的“孤儿”回收站条目。
            try:
                shutil.move(str(destination), str(source))
            except OSError:
                logger.exception("回收站元数据写入失败且无法恢复原文件：%s", source)
            raise
        _cache.pop(str(source), None)
        invalidate_scan_cache(folder_structure=True)
    return {
        "original_path": rel,
        "trash_path": destination.relative_to(config.TRASH_DIR).as_posix(),
        "deleted_at": metadata["deleted_at"],
        "kind": metadata["kind"],
    }


def list_deleted_questions() -> list[dict]:
    out = []
    if not config.TRASH_DIR.exists():
        return out
    for path in config.TRASH_DIR.glob("*.md"):
        try:
            meta, body = _read_raw(path)
        except Exception:
            continue
        if not _is_question_meta(meta):
            continue
        rec = _to_record(path, meta, body)
        rec["original_path"] = meta.get("_trash_original_path", "")
        rec["deleted_at"] = meta.get("_trash_deleted_at", "")
        out.append(rec)
    out.sort(key=lambda r: r["deleted_at"], reverse=True)
    return out


def restore_question(qid: str):
    for path in config.TRASH_DIR.glob("*.md"):
        try:
            meta, body = _read_raw(path)
        except Exception:
            continue
        if not _is_question_meta(meta):
            continue
        if str(meta.get("id")) != str(qid):
            continue
        original = meta.get("_trash_original_path") or f"{qid}.md"
        dst = config.BANK_DIR / original
        if dst.exists():
            dst = config.BANK_DIR / f"{PurePosixPath(original).stem}_restored_{_new_id()}.md"
        meta.pop("_trash_original_path", None)
        meta.pop("_trash_deleted_at", None)
        dst.parent.mkdir(parents=True, exist_ok=True)
        with _write_lock:
            _write_raw(path, meta, body)
            shutil.move(str(path), str(dst))
            invalidate_scan_cache()
        return
    raise KeyError(qid)


def purge_question(qid: str):
    for path in config.TRASH_DIR.glob("*.md"):
        try:
            meta, body = _read_raw(path)
        except Exception:
            continue
        if not _is_question_meta(meta):
            continue
        if str(meta.get("id")) == str(qid):
            refs = _refs_in(meta, body)
            path.unlink()
            purge_orphan_images(refs)
            return


def empty_recycle_bin():
    if not config.TRASH_DIR.exists():
        return
    refs: set[str] = set()
    for entry in config.TRASH_DIR.iterdir():
        if entry.name == "_assets":
            continue
        refs |= _refs_under(entry)
        if entry.is_dir():
            shutil.rmtree(entry)
        else:
            entry.unlink()
    purge_orphan_images(refs)


# ---------------------------------------------------------------------------
# 单字段更新 / 图片布局 / 星标 / 勾选
# ---------------------------------------------------------------------------


def set_difficulty(qid: str, difficulty: str):
    _update_meta_fields(qid, {"difficulty": difficulty})


def set_type(qid: str, qtype: str):
    _update_meta_fields(qid, {"type": qtype})


def update_question_fields_many(ids: list[str], *, qtype: str | None = None,
                                difficulty: str | None = None,
                                starred: bool | None = None,
                                source: str | None = None,
                                note: str | None = None,
                                append_note: bool = False) -> list[str]:
    """批量修改题目属性；None 表示保持原值，空字符串表示明确清空。

    备注位于 Markdown 正文分区，不能当作 frontmatter 字段写入。这里对每道题只做
    一次读写，并继续通过 ``_replace_note_section`` 保留未知自定义分区。
    """
    updated = []
    with _write_lock:
        for qid in dict.fromkeys(str(item) for item in ids if item):
            rec = get_question(qid)
            if not rec:
                continue
            path = config.BANK_DIR / rec["path"]
            meta, raw_body = _read_raw(path)
            if qtype is not None:
                meta["type"] = qtype
            if difficulty is not None:
                meta["difficulty"] = difficulty
            if starred is not None:
                meta["starred"] = bool(starred)
            if source is not None:
                meta["source"] = source

            body = raw_body
            if note is not None:
                next_note = note
                if append_note and rec["note"] and note:
                    next_note = f"{rec['note'].rstrip()}\n\n{note}"
                extra = _replace_note_section(rec["extra_sections"], next_note)
                body = _join_sections(rec["body"], rec["solution"], extra)
            meta["updated"] = _now_iso()
            _write_raw(path, meta, body)
            updated.append(qid)
    return updated


def set_img_align(qid: str, align: str | None):
    """设置首图（index 0）的水平位置：left/center/right，空清除。"""
    _update_meta_fields(qid, {"img_align": align or ""})


def set_img_width(qid: str, width):
    """设置首图（index 0）的宽度百分比（10-100），空/None 清除。"""
    _update_meta_fields(qid, {"img_width": int(width) if width not in (None, "") else None})


def set_img_split(qid: str, mode: str | None, field: str = "body"):
    """图片位置模式；旧真值（True/1）仍视为 "opts"。

    **关掉写 "off"，不写空**：空值（frontmatter 里缺这一项）表示「用户从未设过」，
    `exporter.resolve_split` 会给带图选择题默认 "full"、`plan_figs` 会给四图选择题
    默认配对。关掉也写空的话默认值立刻把它变回开，表现就是「点了没反应」。
    "off" 在 `_norm_split` 里同样归 None（关），只是保留了「这是用户明确选的」。
    与线上版 db.set_img_split 逐条同义。
    """
    if field == "solution":
        _update_meta_fields(qid, {"sol_img_split": "full" if mode == "full" else "off"})
        return
    if mode in ("opts", "full", "sub", "between", "after", "pair"):
        val = mode
    elif mode:                 # 兼容旧布尔真值
        val = "opts"
    else:
        val = "off"            # 明确关闭，区别于「没设过」的空
    _update_meta_fields(qid, {"img_split": val})


def set_img_layout(qid: str, index: int, align=None, width=None, stack=None,
                   field="body"):
    """设置第 index 张图（0 起）的宽度/对齐/堆叠，落进 img_layouts JSON 列表。

    align/width 传 None 表示"本次不动这一项"，传 "" 表示"清除该项"。
    stack 是布尔（True=这一组图上下堆叠，False=清除标记回到默认并排），只对
    「连续两图」组的**首图**有意义（见 exporter.plan_figs 的分组）。存进同一个
    条目而不另开字段：并排/堆叠是逐图设置，与 w/align 同源。
    index==0 时一并回写标量 img_align/img_width（供无 img_layouts 的旧路径读取）。

    field="solution" 时改的是 sol_img_layouts（解析里的图），**序号与题干各自
    独立编号**。解析侧不回写标量：img_align/img_width 是题干的兜底，被解析的
    设置覆盖会让题干配图跟着变。
    """
    rec = get_question(qid)
    if not rec:
        return
    key = "sol_img_layouts" if field == "solution" else "img_layouts"
    items = [dict(it) for it in rec[key] if isinstance(it, dict)]
    cur = None
    for it in items:
        if it.get("i") == index:
            cur = it
            break
    if cur is None:
        cur = {"i": index}
        items.append(cur)

    if width is not None:
        if width == "":
            cur.pop("w", None)
        else:
            cur["w"] = int(width)
    if align is not None:
        if align == "":
            cur.pop("align", None)
        else:
            cur["align"] = align
    if stack is not None:
        if stack:
            cur["stack"] = True
        else:
            cur.pop("stack", None)      # 回到默认并排，不留 false 占位

    items = [it for it in items
             if it.get("w") is not None or it.get("align") or it.get("stack")]
    items.sort(key=lambda it: it.get("i", 0))

    fields = {key: items}
    if index == 0 and key == "img_layouts":
        if width is not None:
            fields["img_width"] = int(width) if width != "" else None
        if align is not None:
            fields["img_align"] = align or ""
    _update_meta_fields(qid, fields)


def _swap_layout_items(items, i: int, j: int) -> list[dict]:
    """把逐图设置表里 i 与 j 两个条目的 `i` 字段互换。

    表结构是 `[{"i": 序号, ...}, ...]`，序号即图片在正文里的出现顺序。交换正文里
    两个图引用时这张表必须跟着换，否则宽度/对齐/原图记录会张冠李戴。
    """
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        it = dict(it)
        if it.get("i") == i:
            it["i"] = j
        elif it.get("i") == j:
            it["i"] = i
        out.append(it)
    out.sort(key=lambda it: it.get("i", 0))
    return out


def swap_images(qid: str, i: int, j: int, text: str, field: str = "body"):
    """交换第 i、j 张图：写入新正文，并同步所有逐图元数据的序号。

    新正文由调用方给（图引用的交换是纯文本操作，见 qrender.swap_image_refs），
    这里只保证**几处一起改**：序号 i 是多处共享的不变量（`.body img` 遍历序、
    导出侧图片文件名列表下标、img_layouts[].i、img_originals[].i、
    img_versions[].i、
    image-redraw.js 的 pickedIndex），任一处不同步就会让宽度/对齐/重绘原图错位
    到别的图上。所以一次读写落盘，不拆成两次 _update_meta_fields。

    field="solution" 时改的是解析：新正文写进解析段，序号换的是 sol_img_layouts
    ——题干与解析的图片各自独立编号，互不影响；img_originals 只记题干侧的重绘
    原图，故解析路径不动它。
    """
    rec = get_question(qid)
    if not rec:
        return
    path = config.BANK_DIR / rec["path"]
    meta, _ = _read_raw(path)
    key = "sol_img_layouts" if field == "solution" else "img_layouts"
    meta[key] = _swap_layout_items(list(meta.get(key) or []), i, j)
    if field != "solution":
        meta["img_originals"] = _swap_layout_items(
            list(meta.get("img_originals") or []), i, j)
        meta["img_versions"] = _swap_layout_items(
            list(meta.get("img_versions") or []), i, j)
    if field == "solution":
        full_body = _join_sections(rec["body"], text, rec["extra_sections"])
    else:
        full_body = _join_sections(text, rec["solution"], rec["extra_sections"])
    meta["updated"] = _now_iso()
    with _write_lock:
        _write_raw(path, meta, full_body)


# ---------------------------------------------------------------------------
# AI 重绘的原图备份（img_originals）
#
# 存的是文件名而不是路径，与正文里 ![[文件名]] 的写法一致。原图文件本身**一律不删**
# ——重绘换掉正文引用后，那张 jpg 仍然躺在 _assets 里，所以"还原"只是把引用换回去，
# 随时可退，不存在"退不回来"的状态。
# ---------------------------------------------------------------------------

def remember_img_original(qid: str, index: int, filename: str):
    """记下第 index 张图的原始文件名。**只在首次写入时锁定**。

    重绘可以连点多次（用户对第一版不满意再生成），第二次重绘时正文里已经是
    tikz_*.svg 了；如果每次都覆盖这条记录，原图就永久丢了引用，"还原"会把用户
    退回到上一版 TikZ 而不是最初的照片。故首次写入即锁定，之后不再更新。
    """
    rec = get_question(qid)
    if not rec:
        return
    items = [dict(it) for it in rec["img_originals"] if isinstance(it, dict)]
    for it in items:
        if it.get("i") == index:
            return          # 已锁定，保持最初那张
    items.append({"i": index, "orig": filename})
    items.sort(key=lambda it: it.get("i", 0))
    _update_meta_fields(qid, {"img_originals": items})
    remember_img_version(qid, index, filename, kind="original")


class ImageVersionError(ValueError):
    """图片版本不存在，或当前操作会破坏题目仍在使用的资源。"""


def list_img_versions(qid: str, index: int) -> list[dict]:
    """列出题干第 index 张图的版本，current 由正文引用实时推导。"""
    rec = get_question(qid)
    if not rec:
        raise ImageVersionError(f"题目不存在：{qid}")
    refs = [m.group(1).strip() for m in _IMAGE_REF_RE.finditer(rec["body"] or "")]
    try:
        current = refs[index]
    except (IndexError, TypeError):
        raise ImageVersionError(f"图片序号越界：{index}") from None
    out = []
    for item in rec.get("img_versions", []):
        if not isinstance(item, Mapping) or item.get("i") != index:
            continue
        row = dict(item)
        row["current"] = row.get("name") == current
        out.append(row)
    return out


def remember_img_version(qid: str, index: int, filename: str, *,
                         kind: str = "", created: str = "", model: str = "",
                         prompt: str = "") -> dict:
    """登记一个版本；同时把旧题目的当前图和原图备份纳入版本表。"""
    try:
        i = int(index)
    except (TypeError, ValueError):
        raise ImageVersionError("图片序号无效") from None
    name = str(filename or "").strip()
    if i < 0 or not name or "/" in name or "\\" in name or name.startswith("."):
        raise ImageVersionError("图片版本文件名无效")
    with _write_lock:
        rec = get_question(qid)
        if not rec:
            raise ImageVersionError(f"题目不存在：{qid}")
        path = config.BANK_DIR / rec["path"]
        meta, body = _read_raw(path)
        versions = _merge_img_versions(meta, body)
        row = next((item for item in versions
                    if item.get("i") == i and item.get("name") == name), None)
        if row is None:
            row = {"i": i, "name": name,
                   "kind": _image_version_kind(name, kind),
                   "created": (created or _now_iso())[:40]}
            versions.append(row)
        else:
            row["kind"] = _image_version_kind(name, kind or row.get("kind", ""))
            if created and not row.get("created"):
                row["created"] = str(created)[:40]
        if model and not row.get("model"):
            row["model"] = str(model)[:120]
        if prompt and not row.get("prompt"):
            row["prompt"] = str(prompt)[:2000]
        if meta.get("img_versions") != versions:
            meta["img_versions"] = versions
            meta["updated"] = _now_iso()
            _write_raw(path, meta, body)
        return dict(row)


def ensure_img_versions(qid: str) -> list[dict]:
    """把旧题的正文/原图备份迁移到 img_versions，并返回完整版本表。"""
    with _write_lock:
        rec = get_question(qid)
        if not rec:
            raise ImageVersionError(f"题目不存在：{qid}")
        path = config.BANK_DIR / rec["path"]
        meta, body = _read_raw(path)
        versions = _merge_img_versions(meta, body)
        if meta.get("img_versions") != versions:
            meta["img_versions"] = versions
            meta["updated"] = _now_iso()
            _write_raw(path, meta, body)
        return versions


def delete_img_version(qid: str, index: int, filename: str) -> dict:
    """删除一个非当前、非原图版本，并在全库无引用时删除对应资源文件。"""
    name = str(filename or "").strip()
    with _write_lock:
        rec = get_question(qid)
        if not rec:
            raise ImageVersionError(f"题目不存在：{qid}")
        refs = [m.group(1).strip() for m in _IMAGE_REF_RE.finditer(rec["body"] or "")]
        if not isinstance(index, int) or index < 0 or index >= len(refs):
            raise ImageVersionError("图片序号越界")
        versions = _merge_img_versions(_read_raw(
            config.BANK_DIR / rec["path"])[0], rec["body"])
        target = next((item for item in versions
                       if item.get("i") == index and item.get("name") == name), None)
        if target is None:
            raise ImageVersionError("图片版本不存在或已删除")
        if name == refs[index]:
            raise ImageVersionError("当前正在使用这个版本，请先切换到其他版本")
        if target.get("kind") == "original":
            raise ImageVersionError("原图版本不能删除")
        path = config.BANK_DIR / rec["path"]
        meta, body = _read_raw(path)
        meta["img_versions"] = [item for item in versions
                                 if not (item.get("i") == index
                                         and item.get("name") == name)]
        meta["updated"] = _now_iso()
        _write_raw(path, meta, body)

    candidates = {name}
    if name.lower().endswith(".svg"):
        candidates.add(name[:-4] + ".pdf")
    # purge_orphan_images 会覆盖当前题库、其他已登记题库和回收站，只有全库无引用时才删。
    removed_files = purge_orphan_images(candidates)
    return {"metadata_deleted": True, "removed_files": removed_files,
            "file_deleted": removed_files > 0}


def get_img_original(qid: str, index: int) -> str:
    """取第 index 张图的原始文件名；没有记录返回空串。"""
    rec = get_question(qid)
    if not rec:
        return ""
    for it in rec["img_originals"]:
        if isinstance(it, dict) and it.get("i") == index:
            return str(it.get("orig") or "")
    for it in rec.get("img_versions", []):
        if (isinstance(it, dict) and it.get("i") == index
                and it.get("kind") == "original"):
            return str(it.get("name") or "")
    return ""


def forget_img_original(qid: str, index: int):
    """删掉第 index 张图的备份记录。

    还原之后必须删：否则"还原原图"按钮一直亮着，用户点了却没有任何变化
    （正文已经是原图了），像坏了一样。
    """
    rec = get_question(qid)
    if not rec:
        return
    items = [dict(it) for it in rec["img_originals"]
             if isinstance(it, dict) and it.get("i") != index]
    _update_meta_fields(qid, {"img_originals": items})


def toggle_starred(qid: str):
    rec = get_question(qid)
    if rec:
        _update_meta_fields(qid, {"starred": not rec["starred"]})


def set_starred_many(ids: list[str], starred: bool):
    for qid in ids:
        _update_meta_fields(qid, {"starred": starred})


def toggle_selected(qid: str) -> bool:
    with _selected_lock:
        if qid in _selected:
            _selected.discard(qid)
            _save_selected_unlocked()
            return False
        _selected.add(qid)
        _save_selected_unlocked()
        return True


def clear_selected():
    with _selected_lock:
        _selected.clear()
        _save_selected_unlocked()


def select_ids(ids: list[str]):
    with _selected_lock:
        _selected.update(ids)
        _save_selected_unlocked()


def select_all(ids: list[str]):
    with _selected_lock:
        _selected.update(ids)
        _save_selected_unlocked()


def count_selected() -> int:
    with _selected_lock:
        return len(_selected)


def selected_ids() -> list[str]:
    """直接返回勾选篮里的 id，不为几十个 id 扫描全部题目 Markdown。"""
    with _selected_lock:
        return list(_selected)


def reorder(ids: list[str]):
    for i, qid in enumerate(ids):
        _update_meta_fields(qid, {"order": float(i)})


_ORDER_RENORMALIZE_GAP = 1e-9


def _write_record_order(record: dict, target_dir: Path, order: float) -> None:
    """只改一份已确认直属当前题集的题卡顺序。调用方必须持有写锁。"""
    path = config.BANK_DIR / record["path"]
    try:
        resolved = path.resolve()
    except OSError as exc:
        raise ValueError("题卡路径无法读取") from exc
    if (resolved.parent != target_dir or not path.is_file()
            or path.is_symlink()):
        raise ValueError("题卡已不在指定题集中，请刷新后重试")
    meta, body = _read_raw(path)
    actual_id = str(meta.get("id") or path.stem)
    if actual_id != str(record["id"]):
        raise ValueError("题卡身份已变化，请刷新后重试")
    meta["order"] = float(order)
    meta["updated"] = _now_iso()
    _write_raw(path, meta, body)


def reorder_relative(question_id: str, collection: str, anchor_id: str,
                     placement: str) -> dict:
    """在一个明确的叶子题集内，把题卡放到锚点前或后。

    常规拖动只给被拖题写相邻顺序的中点；相邻值并列、非有限或浮点间隙即将
    耗尽时，才在同一把锁内把这个题集归一为 1..N。整个过程只枚举并写入目标
    目录的直属 Markdown，不扫描或改写其它题集。
    """
    qid = str(question_id or "").strip()
    anchor = str(anchor_id or "").strip()
    folder_id = str(collection or "").strip("/")
    side = str(placement or "").strip().lower()
    if not qid or not anchor:
        raise ValueError("题目 id 和锚点 id 不能为空")
    if qid == anchor:
        raise ValueError("题目不能以自身作为排序锚点")
    if side not in ("before", "after"):
        raise ValueError("placement 只能是 before 或 after")

    with _write_lock:
        target_dir = _checked_folder_path(folder_id, allow_root=False)
        if not target_dir.is_dir():
            raise ValueError("题集不存在")
        try:
            has_child_collection = any(
                child.is_dir()
                and not child.name.startswith(".")
                and child.name not in _RESERVED_BANK_DIRS
                for child in target_dir.iterdir())
        except OSError as exc:
            raise ValueError("题集无法读取") from exc
        if has_child_collection:
            raise ValueError("只能在叶子题集内调整题目顺序")

        records = collection_records_snapshot(folder_id, recursive=False)
        matches = {
            wanted: [record for record in records
                     if str(record.get("id") or "") == wanted
                     and record.get("folder") == folder_id]
            for wanted in (qid, anchor)
        }
        if len(matches[qid]) != 1 or len(matches[anchor]) != 1:
            raise ValueError("拖动题目和锚点必须直属当前题集")

        ordered = sorted(records, key=_SORT_KEYS["custom"])
        original_ids = [str(record["id"]) for record in ordered]
        dragged = matches[qid][0]
        remaining = [record for record in ordered
                     if str(record.get("id") or "") != qid]
        anchor_index = next(
            index for index, record in enumerate(remaining)
            if str(record.get("id") or "") == anchor)
        insert_at = anchor_index if side == "before" else anchor_index + 1
        desired = remaining[:insert_at] + [dragged] + remaining[insert_at:]
        desired_ids = [str(record["id"]) for record in desired]
        if desired_ids == original_ids:
            return {
                "question_id": qid, "collection": folder_id,
                "anchor_id": anchor, "placement": side,
                "order": float(dragged["order"]), "normalized": False,
                "changed": False,
            }

        previous = desired[insert_at - 1] if insert_at else None
        following = (desired[insert_at + 1]
                     if insert_at + 1 < len(desired) else None)
        left = float(previous["order"]) if previous else None
        right = float(following["order"]) if following else None
        normalize = (
            (left is not None and not math.isfinite(left))
            or (right is not None and not math.isfinite(right))
            or (left is not None and right is not None
                and (right - left <= _ORDER_RENORMALIZE_GAP
                     or not math.isfinite((left + right) / 2.0)))
        )

        if normalize:
            new_order = 0.0
            for index, record in enumerate(desired, start=1):
                candidate = float(index)
                if float(record["order"]) != candidate:
                    _write_record_order(record, target_dir, candidate)
                if str(record["id"]) == qid:
                    new_order = candidate
        else:
            if left is None:
                new_order = right - 1.0
            elif right is None:
                new_order = left + 1.0
            else:
                new_order = (left + right) / 2.0
            if not math.isfinite(new_order):
                # 单侧极值溢出同样回到有界整数序列。
                for index, record in enumerate(desired, start=1):
                    candidate = float(index)
                    if float(record["order"]) != candidate:
                        _write_record_order(record, target_dir, candidate)
                    if str(record["id"]) == qid:
                        new_order = candidate
                normalize = True
            else:
                _write_record_order(dragged, target_dir, new_order)

    return {
        "question_id": qid, "collection": folder_id,
        "anchor_id": anchor, "placement": side,
        "order": new_order, "normalized": normalize, "changed": True,
    }


# ---------------------------------------------------------------------------
# 题目所属文件夹（题目所在目录即其"所属文件夹"，移动=移动文件）
# ---------------------------------------------------------------------------


def collections_of(qid: str) -> list[str]:
    rec = get_question(qid)
    return [rec["folder"]] if rec and rec["folder"] else []


def add_to_collection(qid: str, folder_id: str, *, target_order: float | None = None) -> bool:
    """把题目移动到指定文件夹（一题只能在一个目录下，与目录语义一致）。

    落地时沿用当前文件名，目标目录撞名则添加可读数字后缀。文件名不参与身份认定，
    改名不会改变 id 或图片引用。
    """
    rec = get_question(qid)
    if not rec:
        return
    src = config.BANK_DIR / rec["path"]
    dst_dir = _folder_abspath(folder_id)
    # 已经在目标目录里：什么都不做。这一条必须在取名之前——`_question_filename`
    # 靠 `exists()` 判撞名，而「本题自己」就摊在那儿，会被当成撞名，把
    # `第3题.md` 改成 `第3题_<qid>.md`，每点一次「加入本文件夹」就多一截后缀。
    if src.parent.resolve() == dst_dir.resolve():
        return False
    dst_dir.mkdir(parents=True, exist_ok=True)
    with _write_lock:
        # 同一把锁内取名 + 移动，理由同 create_question。
        dst = _question_filename(
            dst_dir, qid, rec["number"], src.stem)
        # 移入题目应当按本批原顺序追加到目标目录末尾，不能沿用来源目录的 order。
        # 否则两份卷子的第 1 题都带 order=1，目标目录会交错显示。先完整序列化新
        # frontmatter，再移动；写入失败时把原始字节移回来源，避免留下半份题目。
        meta, body = _read_raw(src)
        meta["order"] = float(
            _top_order(folder_id) if target_order is None else target_order)
        updated_text = _render_raw(meta, body)
        original = src.read_bytes()
        shutil.move(str(src), str(dst))
        try:
            dst.write_text(updated_text, encoding="utf-8", newline="\n")
        except Exception:
            try:
                dst.write_bytes(original)
                shutil.move(str(dst), str(src))
            except OSError:
                logger.exception("移动题目回滚失败：%s -> %s", dst, src)
            invalidate_scan_cache()
            raise
        invalidate_scan_cache()
    return True


def move_to_collection(ids: list[str], folder_id: str) -> list[str]:
    """按题库当前顺序把若干题追加到目标文件夹，返回实际移动的 id。"""
    unique_ids = list(dict.fromkeys(str(qid) for qid in ids if qid))
    if not unique_ids:
        return []
    with _write_lock:
        records = records_from_ids(unique_ids)
        records.sort(key=lambda record: (record["order"],) + _tiebreak(record))
        next_order = _top_order(folder_id)
        moved = []
        for record in records:
            if record.get("folder") == folder_id:
                continue
            qid = str(record["id"])
            if add_to_collection(qid, folder_id, target_order=next_order):
                moved.append(qid)
                next_order += 1.0
    return moved


def copy_to_collection(ids: list[str], folder_id: str) -> list[str]:
    """把若干题复制到目标文件夹，返回新题目的 id，原题保持不变。

    副本沿用题干、解析、备注、标签、图片布局和未知自定义 frontmatter；身份、时间、
    排序及自动入库幂等字段必须重新生成。图片位于题库共享资源目录，正文引用可以安全
    共用；孤儿清理会检查全部活跃引用，不会因删除其中一份而误删另一份仍在用的图片。
    """
    unique_ids = list(dict.fromkeys(str(qid) for qid in ids if qid))
    if not unique_ids:
        return []
    target_dir = _folder_abspath(folder_id)
    created: list[str] = []
    written: list[Path] = []
    with _write_lock:
        records = records_from_ids(unique_ids)
        records.sort(key=lambda record: (record["order"],) + _tiebreak(record))
        next_order = _top_order(folder_id)
        try:
            for record in records:
                source_path = config.BANK_DIR / record["path"]
                meta, body = _read_raw(source_path)
                new_id = _new_id()
                now = _now_iso()
                meta = dict(meta)
                meta.update({
                    "id": new_id,
                    "created": now,
                    "updated": now,
                    "order": next_order,
                })
                for key in (
                        "_quizforge_import_scope", "_quizforge_import_index",
                        "_trash_original_path", "_trash_deleted_at"):
                    meta.pop(key, None)
                target = _question_filename(
                    target_dir, new_id, record.get("number"),
                    source_path.stem)
                _write_raw(target, meta, body)
                written.append(target)
                created.append(new_id)
                next_order += 1.0
        except Exception:
            for path in reversed(written):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    logger.exception("复制题目回滚失败：%s", path)
            invalidate_scan_cache()
            raise
    return created


def remove_from_collection(qid: str, folder_id: str = ""):
    """从文件夹移出 = 移到题库根目录。"""
    rec = get_question(qid)
    if not rec or rec["folder"] != folder_id:
        return
    add_to_collection(qid, "")


# ---------------------------------------------------------------------------
# 原卷附件（「一并保存原卷」）
#
# 服务器版为此建了一张 collection_papers 表 + papers.store 的独立目录 + 孤儿
# 文件清理，因为它的「文件夹」只是一行数据库记录，附件无处可放。本地的文件夹
# **就是 vault 里的真目录**，原卷直接 copy 进去即可：文件浏览器就是入口、
# Obsidian 自带 PDF 阅读器能点开、删文件夹时原卷跟着进 .trash、导出/备份整个
# 目录时它自然在内。那张表和那套清理逻辑在这个模型下没有对应物。
# ---------------------------------------------------------------------------

# 原卷不是 Markdown，不会被 _scan 当成题目；也不放 _assets（那儿是正文引用的
# 图片，且被 _skip_rel 排除）。就摊在题所在的那个目录里，跟题目并列。
_PAPER_MAX_STEM = 60
_MARKDOWN_SUFFIXES = frozenset({".md", ".markdown"})


def paper_filename(display_name: str, kind: str = "exam") -> str:
    """原卷落盘用的文件名：清掉路径与保留字符，过长的截断，保留扩展名。

    名字来自**上传文件名**，与 safe_folder_name 同样的理由必须收：它会被拼进
    题库目录。这里额外保留扩展名——Obsidian 靠扩展名决定能不能点开预览，
    丢了扩展名的 PDF 在 vault 里就是个打不开的死文件。
    """
    # 兜底名按 kind 分：两份都退化成「原卷」时，同一个文件夹里的题干和答案会撞名，
    # 后存那份被加上随机后缀，谁是答案就再也看不出来了。
    fallback = "答案" if kind == "solution" else "原卷"
    name = (display_name or "").strip()
    suffix = PurePosixPath(name).suffix
    stem = name[: len(name) - len(suffix)] if suffix else name
    stem = safe_folder_name(stem) or fallback
    suffix = safe_folder_name(suffix.lstrip("."))
    return f"{stem[:_PAPER_MAX_STEM]}.{suffix}" if suffix else stem[:_PAPER_MAX_STEM]


def store_paper(src_path, folder_id: str, display_name: str,
                kind: str = "exam") -> str:
    """把一份原卷**复制**（不是移动）进 folder_id 目录，返回落盘文件名。

    复制而非移动：批量审核期间那份临时文件还要供「重新转换」和原文对照
    （`/convert/file/<job_id>`）用，移走了那两个功能当场坏掉。整批结束时
    `_maybe_finish_batch` 会统一删临时文件，这里留下的副本不受影响。

    同名同内容时直接复用，保证重复提交幂等；同名但内容不同则明确报冲突，既不
    覆盖也不悄悄加随机后缀。历史行为会生成随机后缀，批量重跑后很难判断哪一份
    才是基线原卷，也无法自动核对「源文件数 = 已保存原卷数」。
    """
    src = Path(src_path)
    if not src.is_file():
        raise FileNotFoundError(str(src))
    dst_dir = _folder_abspath(folder_id)
    dst_dir.mkdir(parents=True, exist_ok=True)
    name = paper_filename(display_name, kind)
    dst = dst_dir / name
    if dst.exists():
        def _digest(path: Path) -> str:
            h = hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    h.update(chunk)
            return h.hexdigest()

        if _digest(src) == _digest(dst):
            return dst.name
        raise FileExistsError(f"原卷同名但内容不同：{name}")
    with _write_lock:
        shutil.copy2(str(src), str(dst))
    return dst.name


def _paper_kind(name: str) -> str:
    """从文件名猜是题干卷还是解析卷。

    服务器版把 kind 存在 `collection_papers.kind` 列里，本地没有那张表、也刻意不
    另建索引文件（vault 里多一个元数据文件，用户在 Obsidian 里看见只会困惑，而且
    手动拖进来的原卷不会有对应的行，那种「一半有记录一半没有」的状态最难维护）。
    所以按 `paper_filename` / `_solution_display_name` 写出来的名字反推——那两处
    是本地唯一的命名出口，一致的。手动拖进来的文件默认算题干卷，用户在面板上看得
    见 kind、要改名一眼就知道怎么改。
    """
    stem = Path(name).stem
    return "solution" if ("答案" in stem or "解析" in stem) else "exam"


def list_papers(folder_id: str) -> list[dict]:
    """列出文件夹（含后代）里的原卷附件。

    **口径与题目列表一致**（`list_questions` 的 `collection` 也含后代子树）：
    父文件夹上看到的题来自子文件夹时，对应的原卷也该在同一个面板里看得见，否则
    用户得逐级点进去找。返回项里的 `folder` 标出它实际在哪一级。

    原卷 = 目录里的**非 Markdown 普通文件**。判据是排除法而不是白名单扩展名：用户可能
    往里放 .zip 讲义、.png 扫描页，列不出来只会让人以为文件丢了。跳过点开头的
    文件/目录与 `_assets`（口径同 `_skip_rel`），跳过 `.md` / `.markdown`
    （题卡或资料文档）。
    """
    root = _folder_abspath(folder_id)
    if not root.is_dir():
        return []
    out: list[dict] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() in _MARKDOWN_SUFFIXES:
            continue
        try:
            rel = path.relative_to(config.BANK_DIR)
        except ValueError:
            continue
        if _skip_rel(rel) or path.name.startswith("."):
            continue
        folder = (str(PurePosixPath(rel.parent.as_posix()))
                  if rel.parent != Path(".") else "")
        try:
            size = path.stat().st_size
        except OSError:
            continue
        out.append({
            # id = 相对题库根的 posix 路径。**只在服务端用它反查**，见 paper_abspath。
            "id": str(rel.as_posix()),
            "filename": path.name,
            "folder": folder,
            "kind": _paper_kind(path.name),
            "byte_size": size,
        })
    out.sort(key=lambda p: (p["folder"], p["filename"]))
    return out


def paper_abspath(paper_id: str) -> Path | None:
    """原卷 id（相对路径）→ 绝对路径。越界/不存在/Markdown 一律返回 None。

    **这是原卷的唯一取路入口，不许别处自己拼**：id 来自请求参数，`../` 拼进去就是
    一个任意文件读取/删除接口，而软件版无鉴权，这条尤其不能松（与 `/outfile/<token>`
    同一条规矩）。判据用 `resolve()` 后的祖先关系，不是字符串前缀比较——后者被
    符号链接和 `..` 绕得过去。
    """
    if not paper_id:
        return None
    try:
        target = (config.BANK_DIR / PurePosixPath(paper_id)).resolve()
        root = config.BANK_DIR.resolve()
    except OSError:
        return None
    if root != target and root not in target.parents:
        return None
    if not target.is_file() or target.suffix.lower() in _MARKDOWN_SUFFIXES:
        return None
    rel = target.relative_to(root)
    if _skip_rel(rel) or target.name.startswith("."):
        return None
    return target


def remove_paper(paper_id: str) -> bool:
    """把一份原卷移入回收站，删掉了返回 True。"""
    target = paper_abspath(paper_id)
    if target is None:
        return False
    try:
        trash_file(target, kind="paper")
    except (OSError, ValueError):
        return False
    return True


# ---------------------------------------------------------------------------
# 图片资产
# ---------------------------------------------------------------------------


def save_image(qid: str, index: int, data: bytes, ext: str) -> str:
    """保存图片到 _assets/，返回 Obsidian 嵌入语法 `![[<id>_N.ext]]`。"""
    ext = ext.lstrip(".").lower() or "png"
    filename = f"{qid}_{index}.{ext}"
    config.ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    (config.ASSETS_DIR / filename).write_bytes(data)
    return f"![[{filename}]]"


def asset_path(filename: str) -> Path:
    return config.ASSETS_DIR / filename


# ---------------------------------------------------------------------------
# 孤儿图片清理
#
# `_assets/` 是**全库共用的一个扁平目录**，删题时不顺手清图，图片就只增不减
# （用户报的「图片堆积」）。清理只在**彻底删除**时做（回收站里的三个入口），
# 软删不动图——那时正文还在 `.trash` 里，恢复回来得能看见图。
#
# **按正文里的 `![[...]]` 引用清，不能按文件名前缀猜**：`_assets` 里有两套命名，
# `save_image` 写的是 `<qid>_<N>.<ext>`（能按 qid 前缀找），而 OCR 导入的图由
# `converter._intercept_images` 写成 `<原卷名>_<MinerU 哈希>.<ext>`，跟 qid 毫无
# 关系——实测线上 vault 里 56 张图**全是**后一种。按 qid 前缀扫等于一张都清不掉。
# ---------------------------------------------------------------------------

# 与 qrender._QIMG_RE 同一口径（Obsidian 嵌入，可带 `|宽度` 后缀）。刻意各写一份：
# 那边是渲染用的、要拿宽度，这里只要文件名，共用会让任一侧改捕获组时另一侧静默错。
_EMBED_RE = re.compile(r"!\[\[([^\]\|]+)(?:\|[^\]]*)?\]\]")
_ASSET_FILE_EXTS = frozenset({
    ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".avif", ".svg", ".pdf",
})
# OCR／重绘确认时会先落图片再写题目。跨进程写入没有共用 Python 锁，因此全库清理
# 保守跳过最近五分钟的未引用文件，避免正好撞上另一个题库窗口的入库瞬间。
_ASSET_GC_GRACE_NS = 5 * 60 * 1_000_000_000


class AssetAuditError(RuntimeError):
    """图片审计无法完整覆盖全部已登记题库；此时必须拒绝删除。"""


def _refs_in(meta: dict, body: str) -> set[str]:
    """一份题目文件引用到的图片文件名集合。

    除了正文里的 `![[...]]`，还要算上 `img_originals` 里记的重绘原图——那些文件名
    已经不在正文里了（正文换成了 tikz_*.svg），但「还原」要靠它们，不能当孤儿删掉。
    重绘产出的 svg 还带一个同名 pdf（见 tikz_render），一并算进来。
    """
    out = {Path(m.group(1)).name for m in _EMBED_RE.finditer(body or "")}
    for it in (meta.get("img_originals") or []):
        if isinstance(it, dict) and it.get("orig"):
            out.add(Path(str(it["orig"])).name)
    for it in (meta.get("img_versions") or []):
        if isinstance(it, dict) and it.get("name"):
            out.add(Path(str(it["name"])).name)
    for name in list(out):
        if name.lower().endswith(".svg"):
            out.add(name[:-4] + ".pdf")
    return out


def _refs_under(entry: Path) -> set[str]:
    """一个 .md 文件、或一个目录（子树里所有 .md）引用到的图片文件名。"""
    out: set[str] = set()
    paths = [entry] if entry.is_file() else list(entry.rglob("*.md"))
    for p in paths:
        if p.suffix.lower() != ".md":
            continue
        try:
            meta, body = _read_raw(p)
        except Exception:
            continue
        out |= _refs_in(meta, body)
    return out


def _live_refs() -> set[str]:
    """当前**所有还活着的**引用：题库里的题 + 回收站里等待恢复的一切。

    回收站必须算进来（题目 md、软删文件夹的整棵子树）。漏了它，删掉一道题会把
    「另一道软删的题还在用」的图一起清掉，用户点恢复得到一道图全裂的题——而且
    不可逆。宁可留下几张暂时清不掉的图，也不能删错。
    """
    out: set[str] = set()
    for rec in _all_records():
        path = config.BANK_DIR / rec["path"]
        try:
            meta, body = _read_raw(path)
        except Exception:
            continue
        out |= _refs_in(meta, body)
    if config.TRASH_DIR.exists():
        for entry in config.TRASH_DIR.iterdir():
            if entry.name == "_assets":
                continue
            out |= _refs_under(entry)
    return out


def _registered_bank_roots() -> list[Path]:
    """返回当前题库及 desktop.json 中全部已登记题库；任一不可用即失败。

    图片现在可以跨题库共享。删除判定若悄悄跳过断开的盘符或损坏配置，就可能把那一
    库仍在使用的图片当孤儿永久删除，所以这里采用 fail-closed 语义。
    """
    roots: list[Path] = [config.BANK_DIR.resolve()]
    desktop_config = config.DATA_DIR / "desktop.json"
    if desktop_config.is_file():
        try:
            value = json.loads(desktop_config.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AssetAuditError(f"桌面题库配置无法读取：{exc}") from exc
        if not isinstance(value, dict):
            raise AssetAuditError("桌面题库配置格式无效")
        entries = value.get("banks") or []
        if not isinstance(entries, list):
            raise AssetAuditError("桌面题库列表格式无效")
        for entry in entries:
            raw = entry.get("path") if isinstance(entry, dict) else entry
            candidate = Path(str(raw or "").strip()).expanduser()
            if not candidate.is_absolute():
                raise AssetAuditError(f"题库列表含无效路径：{raw}")
            try:
                roots.append(candidate.resolve())
            except (OSError, RuntimeError, ValueError) as exc:
                raise AssetAuditError(f"题库路径无法解析：{raw}") from exc

    result: list[Path] = []
    seen: set[str] = set()
    unavailable: list[str] = []
    for root in roots:
        key = os.path.normcase(str(root))
        if key in seen:
            continue
        seen.add(key)
        try:
            available = root.is_dir() and not _is_link_or_junction(root)
        except OSError:
            available = False
        if not available:
            unavailable.append(str(root))
        else:
            result.append(root)
    if unavailable:
        preview = "；".join(unavailable[:3])
        suffix = " 等" if len(unavailable) > 3 else ""
        raise AssetAuditError(f"有 {len(unavailable)} 个已登记题库不可访问：{preview}{suffix}")
    return result


def _is_link_or_junction(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        check = getattr(path, "is_junction", None)
        return bool(check and check())
    except OSError:
        return True


def _registered_markdown_paths(roots: list[Path]):
    """遍历全部题库 Markdown，父子题库重叠时按真实路径去重。

    `.trash`、`_backups` 和 `_handouts` 仍计入引用：回收站要能恢复，安全刷新备份与
    讲义也不能因清理图片而损坏。只跳过各级 `_assets`、其他点目录和链接目录。
    """
    seen: set[str] = set()
    errors: list[OSError] = []

    def onerror(exc: OSError):
        errors.append(exc)

    # 实际配置常同时登记公共 vault 与其“数学／物理”子目录。先保留最外层根即可
    # 完整覆盖它们；否则虽然后面按真实文件去重，仍会把一万多个目录项白走一遍。
    scan_roots: list[Path] = []
    for root in sorted(roots, key=lambda path: (len(path.parts), str(path).casefold())):
        if any(parent == root or parent in root.parents for parent in scan_roots):
            continue
        scan_roots.append(root)

    for root in scan_roots:
        for current, dirs, files in os.walk(root, topdown=True, onerror=onerror,
                                            followlinks=False):
            current_path = Path(current)
            kept_dirs = []
            for name in dirs:
                candidate = current_path / name
                if name == "_assets" or (name.startswith(".") and name != ".trash"):
                    continue
                if _is_link_or_junction(candidate):
                    continue
                kept_dirs.append(name)
            dirs[:] = kept_dirs
            for name in files:
                if not name.lower().endswith(".md"):
                    continue
                path = current_path / name
                if _is_link_or_junction(path) or not path.is_file():
                    continue
                try:
                    resolved = path.resolve()
                except (OSError, RuntimeError) as exc:
                    raise AssetAuditError(f"Markdown 路径无法解析：{path}") from exc
                key = os.path.normcase(str(resolved))
                if key in seen:
                    continue
                seen.add(key)
                yield resolved
    if errors:
        raise AssetAuditError(f"题库目录扫描不完整：{errors[0]}")


def _registered_live_refs(candidates: set[str] | None = None) -> tuple[set[str], dict]:
    """收集所有已登记题库的图片引用；读取/解析任一 Markdown 失败便拒绝审计。"""
    roots = _registered_bank_roots()
    # 桌面版运行在 Windows，文件名大小写不敏感；引用的大小写与磁盘不同也仍是
    # 同一张图。审计若按 Python 字符串精确比较，会把仍在使用的图误判成孤儿。
    wanted = ({Path(name).name.casefold() for name in candidates}
              if candidates is not None else None)
    live: set[str] = set()
    markdown_files = 0
    for path in _registered_markdown_paths(roots):
        try:
            # 绝大多数题都有 ``img_originals: []``。若为它们逐份启动 ruamel 解析，
            # 一万多题的图片体检会耗时九十秒；Wiki 引用本来就是纯文本语法，直接扫
            # 原文即可。只有确实存在非空重绘原图元数据时才解析 frontmatter。
            text = path.read_text(encoding="utf-8", newline="")
            refs = {Path(match.group(1)).name.casefold()
                    for match in _EMBED_RE.finditer(text)}
            frontmatter = _FM_RE.match(normalize_newlines(text))
            fm_text = frontmatter.group(1) if frontmatter else ""
            if re.search(
                    r"(?m)^(?:img_originals|img_versions)\s*:\s*(?!\[\s*\]\s*$)",
                    fm_text):
                meta, _body = _parse_raw_text(text)
                refs |= {name.casefold() for name in _refs_in(meta, "")}
            for name in list(refs):
                if name.lower().endswith(".svg"):
                    refs.add(name[:-4] + ".pdf")
        except Exception as exc:
            raise AssetAuditError(f"Markdown 无法读取或解析：{path}：{exc}") from exc
        markdown_files += 1
        live |= refs if wanted is None else refs & wanted
        if wanted is not None and wanted <= live:
            break
    return live, {"bank_dirs": [str(root) for root in roots],
                  "markdown_files": markdown_files}


def _asset_files() -> tuple[Path, list[Path], int]:
    assets_dir = config.ASSETS_DIR.resolve()
    if not assets_dir.exists():
        return assets_dir, [], 0
    if not assets_dir.is_dir() or _is_link_or_junction(assets_dir):
        raise AssetAuditError(f"共享图片目录不是普通目录：{assets_dir}")
    files: list[Path] = []
    ignored = 0
    try:
        entries = list(assets_dir.iterdir())
    except OSError as exc:
        raise AssetAuditError(f"共享图片目录无法读取：{exc}") from exc
    for path in entries:
        if (_is_link_or_junction(path) or not path.is_file()
                or path.suffix.lower() not in _ASSET_FILE_EXTS):
            ignored += 1
            continue
        files.append(path)
    return assets_dir, files, ignored


def scan_orphan_assets() -> dict:
    """审计共享图片目录；返回可供二次确认删除的稳定文件快照。"""
    live, stats = _registered_live_refs()
    assets_dir, files, ignored = _asset_files()
    now_ns = time.time_ns()
    orphans: list[dict] = []
    recent_count = 0
    recent_bytes = 0
    asset_names: set[str] = set()
    referenced_files = 0
    for path in files:
        try:
            stat = path.stat()
        except OSError as exc:
            raise AssetAuditError(f"图片状态无法读取：{path}：{exc}") from exc
        asset_names.add(path.name.casefold())
        if path.name.casefold() in live:
            referenced_files += 1
            continue
        if now_ns - stat.st_mtime_ns < _ASSET_GC_GRACE_NS:
            recent_count += 1
            recent_bytes += stat.st_size
            continue
        orphans.append({"name": path.name, "size": stat.st_size,
                        "mtime_ns": stat.st_mtime_ns})
    orphans.sort(key=lambda item: item["name"].lower())
    return {
        "asset_dir": str(assets_dir),
        "bank_dirs": stats["bank_dirs"],
        "bank_count": len(stats["bank_dirs"]),
        "markdown_files": stats["markdown_files"],
        "asset_files": len(files),
        "referenced_files": referenced_files,
        "missing_references": len(live - asset_names),
        "ignored_files": ignored,
        "recent_unreferenced": recent_count,
        "recent_unreferenced_bytes": recent_bytes,
        "orphans": orphans,
        "orphan_count": len(orphans),
        "orphan_bytes": sum(item["size"] for item in orphans),
    }


def delete_scanned_orphan_assets(previous: dict) -> dict:
    """重新全量审计后删除仍为孤儿且自上次扫描后未变化的文件。"""
    if not isinstance(previous, dict):
        raise AssetAuditError("图片扫描快照无效")
    if Path(str(previous.get("asset_dir") or "")).resolve() != config.ASSETS_DIR.resolve():
        raise AssetAuditError("共享图片目录已经变化，请重新扫描")
    old = {item.get("name"): item for item in previous.get("orphans") or []
           if isinstance(item, dict) and item.get("name")}
    current = scan_orphan_assets()
    current_items = {item["name"]: item for item in current["orphans"]}
    removed = 0
    removed_bytes = 0
    changed = 0
    with _write_lock:
        for name, before in old.items():
            item = current_items.get(name)
            if (item is None or item.get("size") != before.get("size")
                    or item.get("mtime_ns") != before.get("mtime_ns")):
                changed += 1
                continue
            target = config.ASSETS_DIR / Path(name).name
            try:
                resolved = target.resolve()
                stat = resolved.stat()
            except OSError:
                changed += 1
                continue
            if (resolved.parent != config.ASSETS_DIR.resolve()
                    or _is_link_or_junction(resolved)
                    or stat.st_size != item["size"]
                    or stat.st_mtime_ns != item["mtime_ns"]):
                changed += 1
                continue
            try:
                resolved.unlink()
                removed += 1
                removed_bytes += stat.st_size
            except OSError as exc:
                logger.warning("孤儿图片删除失败 %s: %s", name, exc)
                changed += 1
    if removed:
        logger.info("跨题库清理孤儿图片 %d 个，共 %d 字节", removed, removed_bytes)
    return {"removed": removed, "removed_bytes": removed_bytes,
            "changed_or_skipped": changed}


def purge_orphan_images(candidates: set[str] | None = None) -> int:
    """删掉共享图片目录里没人引用的图，返回删除数。

    candidates 给定时只考察这几个文件名（刚被彻底删除的那道题引用过的图），不遍历
    整个 `_assets`——一次删题不该顺带清掉别处早就存在的孤儿（AI 重绘换掉引用后
    留下的原图就是这类，见 `remember_img_original` 上方的注释，那是刻意留的）。
    candidates 为 None 时才做全量清理，留给将来的「清理未引用图片」入口用。

    判据一律覆盖 desktop.json 中全部已登记题库，并把回收站、讲义和安全备份都算
    活引用；任一题库不可访问或 Markdown 无法解析时只保留图片，不猜测删除。
    """
    if candidates is None:
        try:
            snapshot = scan_orphan_assets()
            return delete_scanned_orphan_assets(snapshot)["removed"]
        except AssetAuditError as exc:
            logger.warning("全量孤儿图片清理已拒绝：%s", exc)
            return 0
    if not candidates or not config.ASSETS_DIR.exists():
        return 0
    names_by_key = {Path(n).name.casefold(): Path(n).name for n in candidates}
    names = list(names_by_key.values())
    try:
        live, _stats = _registered_live_refs(set(names))
    except AssetAuditError as exc:
        # 彻底删题仍然成功，但图片宁可暂时保留；扫描不完整时绝不猜删。
        logger.warning("候选图片清理已拒绝：%s", exc)
        return 0
    removed = 0
    for name in names:
        if name.casefold() in live:
            continue
        # 名字来自文件内容（用户可在 Obsidian 里手改），当路径片段用之前必须验一次，
        # 否则 `![[../../x]]` 就是个任意文件删除。只删 ASSETS_DIR 直下的普通文件。
        target = config.ASSETS_DIR / name
        try:
            resolved = target.resolve()
        except OSError:
            continue
        if resolved.parent != config.ASSETS_DIR.resolve() or not resolved.is_file():
            continue
        try:
            resolved.unlink()
            removed += 1
        except OSError as e:
            logger.warning("孤儿图片删除失败 %s: %s", name, e)
    if removed:
        logger.info("清理孤儿图片 %d 张", removed)
    return removed


# ---------------------------------------------------------------------------
# 列表查询：过滤 / 排序（Python 侧内存过滤，取代 db.py 的 SQL）
# ---------------------------------------------------------------------------

def _tiebreak(r: dict) -> tuple:
    """并列时的次序：原卷题号 → 入库时间 → 路径。

    **每个排序键都必须缀上它**（2026-08-08 补）。`sorted` 稳定，但它稳的是**输入
    顺序**，而输入来自 `_scan()` 的 `rglob`——由文件系统决定，与题号无关，重扫一次
    还可能变。所以并列项（同一批导入的 order 相同、或同难度/同题型）在页面上的先后
    是随机的，用户看到的就是「顺序丢了」。题号排第一位：一份卷子导进来，用户预期
    的就是按原卷题号排。没有题号的排在有题号的后面（`number` 为 None 时给 +inf）。
    """
    num = r.get("number")
    return (num if isinstance(num, int) else float("inf"),
            r.get("created") or "", r.get("path") or "")


_SORT_KEYS = {
    "custom": lambda r: (r["order"],) + _tiebreak(r),
    # created_desc 是**倒序**排的，缀 `_tiebreak` 会把并列项的题号也一起倒过来
    # （第 10 题排在第 1 题前）。所以这里只用创建时间本身，让同一秒入库的题保持
    # `rglob` 的顺序——总比明确排成倒的强。
    "created_desc": lambda r: r["created"],
    "created_asc": lambda r: (r["created"],) + _tiebreak(r),
    "difficulty": lambda r: ((r["difficulty"] or "0"),) + _tiebreak(r),
    "type": lambda r: (r["type"],) + _tiebreak(r),
    "starred": lambda r: (0 if r["starred"] else 1, r["order"]) + _tiebreak(r),
}
_SORT_REVERSE = {"created_desc": True}


def _source_key(r: dict) -> tuple:
    """题源自然排序；空题源统一放在所有具名题源之后。"""
    source = str(r.get("source") or "").strip()
    return (0, _natural_text_key(source)) if source else (1, ())


def _source_inner_order_key(r: dict) -> tuple:
    """题源组内沿用题目在各自题集中的自定义顺序。"""
    order = r.get("order")
    return (_natural_rel_key(str(r.get("folder") or "")),
            order if isinstance(order, (int, float)) else float("inf")) + _tiebreak(r)


def _folder_subtree_ids(folder_id: str) -> set[str]:
    ids = {folder_id}
    for f in all_collections():
        if f["id"] == folder_id or f["id"].startswith(folder_id + "/"):
            ids.add(f["id"])
    return ids


def list_questions(tags: list[str] | None = None, match: str = "and",
                    qtype: str = "", difficulty: str = "",
                    starred: bool = False, sort: str = "custom",
                    collection: str = "", search: str | SearchQuery = "",
                    selected_only: bool = False,
                    records: list[dict] | None = None) -> list[dict]:
    recs = list(records) if records is not None else _all_records()

    if collection:
        prefix = collection + "/"
        recs = [r for r in recs
                if r["folder"] == collection or r["folder"].startswith(prefix)]
    if selected_only:
        recs = [r for r in recs if r["selected"]]
    tags = [t for t in (tags or []) if t]
    if tags:
        if match == "or":
            recs = [r for r in recs if any(t in r["tags"] for t in tags)]
        else:
            recs = [r for r in recs if all(t in r["tags"] for t in tags)]
    if qtype:
        recs = [r for r in recs if r["type"] == qtype]
    if difficulty:
        recs = [r for r in recs if r["difficulty"] == difficulty]
    if starred:
        recs = [r for r in recs if r["starred"]]
    if search:
        query = (parse_search_query(
            search, allowed_types=config.QUESTION_TYPES,
            allowed_difficulties=config.DIFFICULTIES)
                 if isinstance(search, str) else search)
        recs = [r for r in recs if matches_search(r, query)]

    if sort == "source":
        # 先恢复每个原题集的自定义顺序，再做一次稳定的题源分组；最终排序不附加
        # 题号等二级条件，所以同题源内部不会被重新洗牌。
        recs = sorted(recs, key=_source_inner_order_key)
        recs = sorted(recs, key=_source_key)
    else:
        key = _SORT_KEYS.get(sort, _SORT_KEYS["custom"])
        recs = sorted(recs, key=key, reverse=_SORT_REVERSE.get(sort, False))
    return recs
