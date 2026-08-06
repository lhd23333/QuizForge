"""文件系统题库存储层，替代 quizbank 的 db.py。

设计要点（对应已批准的方案）：
- 每题一个 .md 文件：YAML frontmatter（ruamel 往返解析，未知字段原样保留）+
  Markdown 正文。正文里 `## 解析` 之后的内容识别为解析，其余 `## 标题` 视为
  用户自定义分区，原样传回、绝不解析或丢弃。
- 文件夹 = data/bank/ 下的真实目录；文件夹 id 即相对 BANK_DIR 的 POSIX 相对路径。
- 回收站 = data/bank/.trash/：单题软删平铺存放（用 frontmatter 记原路径供恢复）；
  整个文件夹被删除时，其子树整体移入 .trash/ 下（附 .trash_meta.json 记原位置）。
- 勾选（选入组卷篮）是纯内存态，不落盘，随进程重启清空。
- 图片：data/bank/_assets/ 下扁平存放，正文用 Obsidian 双链 `![[<id>_N.ext]]` 引用。

本模块函数名尽量对齐 quizbank/db.py，方便照原样移植 app.py 的路由逻辑。
"""

from __future__ import annotations

import json
import re
import shutil
import threading
import uuid
from datetime import datetime
from pathlib import Path, PurePosixPath

from ruamel.yaml import YAML

import config

_yaml = YAML()
_yaml.preserve_quotes = True
_yaml.width = 4096  # 别让长题干/公式被自动折行

_write_lock = threading.Lock()

# 正文分区标题行：形如 "## 解析"、"## 备注"
_SECTION_RE = re.compile(r"(?m)^##[ \t]+(.*?)[ \t]*$")

# 内存索引：文件路径(str) -> 已解析记录（含 _path/_mtime）。按 mtime 判断是否需要重解析，
# 使直接在 Obsidian 里改文件也能被下次请求感知到。
_cache: dict[str, dict] = {}

# 勾选篮：纯内存，不持久化。
_selected: set[str] = set()


def init_store():
    """确保题库/回收站/图片目录存在（幂等）。"""
    config.BANK_DIR.mkdir(parents=True, exist_ok=True)
    config.TRASH_DIR.mkdir(parents=True, exist_ok=True)
    config.ASSETS_DIR.mkdir(parents=True, exist_ok=True)


def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


# ---------------------------------------------------------------------------
# 正文分区：stem（题干，首个 `## ` 之前） / 解析 / 其余用户自定义分区（原样透传）
# ---------------------------------------------------------------------------


def _split_sections(body: str) -> tuple[str, list[tuple[str, str]]]:
    """把正文切成 (题干, [(分区标题, 分区内容), ...])。"""
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
    return stem, sections


def _join_sections(stem: str, solution: str | None,
                    extra: list[tuple[str, str]]) -> str:
    """把 (题干, 解析, 其余分区) 拼回正文文本。"""
    parts = [stem.rstrip("\n")]
    if solution and solution.strip():
        parts.append("## 解析\n" + solution.strip("\n"))
    for heading, content in extra:
        parts.append(f"## {heading}\n{content}")
    return "\n\n".join(p for p in parts if p) + "\n"


# ---------------------------------------------------------------------------
# frontmatter 读写
# ---------------------------------------------------------------------------

_FM_RE = re.compile(r"(?s)\A---\r?\n(.*?)\r?\n---\r?\n?(.*)\Z")

# frontmatter 里除了下列已知字段，其余字段（用户手写的自定义字段）原样保留，
# 靠 ruamel 的 round-trip 能力自动做到，无需在代码里枚举。
_KNOWN_DEFAULTS = {
    "type": "",
    "difficulty": "",
    "source": "",
    "tags": [],
    "starred": False,
    "order": 0.0,
    "img_align": "",
    "img_width": None,
    "img_split": "",
    "img_layouts": [],
}


def _read_raw(path: Path) -> tuple[dict, str]:
    text = path.read_text(encoding="utf-8")
    m = _FM_RE.match(text)
    if not m:
        # 没有 frontmatter 的裸 md：当成只有题干的新题，id 用文件名。
        return {}, text
    fm_text, body = m.group(1), m.group(2)
    data = _yaml.load(fm_text) or {}
    return data, body


def _write_raw(path: Path, meta: dict, body: str):
    import io

    buf = io.StringIO()
    _yaml.dump(meta, buf)
    text = "---\n" + buf.getvalue() + "---\n\n" + body.lstrip("\n")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _to_record(path: Path, meta: dict, body: str) -> dict:
    stem, sections = _split_sections(body)
    solution = ""
    extra = []
    for heading, content in sections:
        if heading == "解析" and not solution:
            solution = content
        else:
            extra.append((heading, content))
    rel = path.relative_to(config.BANK_DIR)
    folder = str(PurePosixPath(rel.parent.as_posix())) if rel.parent != Path(".") else ""
    qid = str(meta.get("id") or path.stem)
    rec = {
        "id": qid,
        "path": str(rel.as_posix()),
        "folder": folder,
        "body": stem,
        "solution": solution,
        "extra_sections": extra,
        "type": meta.get("type", ""),
        "difficulty": str(meta.get("difficulty", "") or ""),
        "source": meta.get("source", ""),
        "tags": list(meta.get("tags", []) or []),
        "starred": bool(meta.get("starred", False)),
        "order": float(meta.get("order", 0.0) or 0.0),
        "img_align": str(meta.get("img_align", "") or ""),
        "img_width": (int(meta["img_width"]) if meta.get("img_width") not in (None, "") else None),
        "img_split": str(meta.get("img_split", "") or ""),
        "img_layouts": list(meta.get("img_layouts", []) or []),
        "created": meta.get("created", ""),
        "updated": meta.get("updated", ""),
        "selected": qid in _selected,
    }
    return rec


def _scan() -> dict[str, dict]:
    """扫描 BANK_DIR 下全部 .md（跳过 .trash/_assets），mtime 未变则用缓存。"""
    found: dict[str, dict] = {}
    for path in config.BANK_DIR.rglob("*.md"):
        try:
            rel = path.relative_to(config.BANK_DIR)
        except ValueError:
            continue
        if rel.parts and rel.parts[0] in (".trash", "_assets"):
            continue
        key = str(path)
        mtime = path.stat().st_mtime
        cached = _cache.get(key)
        if cached is not None and cached["_mtime"] == mtime:
            found[key] = cached
            continue
        try:
            meta, body = _read_raw(path)
        except Exception:
            continue
        rec = _to_record(path, meta, body)
        rec["_mtime"] = mtime
        rec["_meta"] = meta
        _cache[key] = rec
        found[key] = rec
    # 清掉已不存在的文件的缓存
    stale = set(_cache) - set(found)
    for k in stale:
        _cache.pop(k, None)
    return found


def _all_records() -> list[dict]:
    return list(_scan().values())


def _find_path_by_id(qid: str) -> Path | None:
    for rec in _all_records():
        if rec["id"] == qid:
            return config.BANK_DIR / rec["path"]
    return None


def get_question(qid: str) -> dict | None:
    return _scan().get(str(_find_path_by_id(qid))) if _find_path_by_id(qid) else None


# ---------------------------------------------------------------------------
# 文件夹（= 真实目录）
# ---------------------------------------------------------------------------


def _folder_abspath(folder_id: str) -> Path:
    """folder_id 是相对 BANK_DIR 的 posix 路径，'' 表示根目录。"""
    if not folder_id:
        return config.BANK_DIR
    return config.BANK_DIR / PurePosixPath(folder_id)


def list_collections_tree() -> list[dict]:
    """返回文件夹树：[{id,name,parent_id,cnt,depth,children:[...]}]，按名称字母序。"""
    counts: dict[str, int] = {}
    for rec in _all_records():
        counts[rec["folder"]] = counts.get(rec["folder"], 0) + 1

    def build(dir_path: Path, parent_id: str, depth: int) -> list[dict]:
        try:
            subdirs = sorted(
                (p for p in dir_path.iterdir()
                 if p.is_dir() and p.name not in (".trash", "_assets")),
                key=lambda p: p.name,
            )
        except FileNotFoundError:
            return []
        nodes = []
        for d in subdirs:
            rel = d.relative_to(config.BANK_DIR).as_posix()
            node = {
                "id": rel,
                "name": d.name,
                "parent_id": parent_id,
                "cnt": counts.get(rel, 0),
                "depth": depth,
                "children": build(d, rel, depth + 1),
            }
            node["cnt"] += sum(c["cnt"] for c in node["children"])
            nodes.append(node)
        return nodes

    return build(config.BANK_DIR, "", 0)


def all_collections() -> list[dict]:
    """扁平化文件夹列表（供下拉框用）。"""
    flat = []

    def walk(nodes):
        for n in nodes:
            flat.append(n)
            walk(n["children"])

    walk(list_collections_tree())
    return flat


def get_collection(folder_id: str) -> dict | None:
    for f in all_collections():
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
    return str((PurePosixPath(parent_id) / safe_name)) if parent_id else safe_name


def rename_collection(folder_id: str, new_name: str) -> str:
    old = _folder_abspath(folder_id)
    new_name = new_name.strip()
    if not new_name:
        raise ValueError("名称不能为空")
    new_path = old.parent / new_name
    if new_path.exists():
        raise ValueError("同名文件夹已存在")
    old.rename(new_path)
    new_id = str(new_path.relative_to(config.BANK_DIR).as_posix())
    return new_id


def move_folder(folder_id: str, new_parent_id: str) -> str:
    src = _folder_abspath(folder_id)
    dst_parent = _folder_abspath(new_parent_id)
    # 防止把文件夹移进自己或自己的子文件夹
    if dst_parent == src or dst_parent.is_relative_to(src):
        raise ValueError("不能移动到自己的子文件夹中")
    dst = dst_parent / src.name
    if dst.exists():
        raise ValueError("目标位置已存在同名文件夹")
    shutil.move(str(src), str(dst))
    return str(dst.relative_to(config.BANK_DIR).as_posix())


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


def purge_collection(trash_id: str):
    target = config.TRASH_DIR / trash_id
    if target.exists():
        shutil.rmtree(target)


# ---------------------------------------------------------------------------
# 标签（无独立标签表，直接扫全库 frontmatter 的 tags 字段汇总）
# ---------------------------------------------------------------------------


def all_tags() -> list[str]:
    seen: dict[str, int] = {}
    for rec in _all_records():
        for t in rec["tags"]:
            seen[t] = seen.get(t, 0) + 1
    return sorted(seen, key=lambda t: (-seen[t], t))


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
    orders = [r["order"] for r in _all_records() if r["folder"] == folder]
    return (max(orders) + 1.0) if orders else 1.0


def create_question(body: str, solution: str = "", qtype: str = "",
                     source: str = "", difficulty: str = "",
                     tags: list[str] | None = None, folder: str = "") -> str:
    qid = _new_id()
    meta = dict(_KNOWN_DEFAULTS)
    meta.update({
        "id": qid,
        "type": qtype,
        "source": source,
        "difficulty": difficulty,
        "tags": list(tags or []),
        "order": _top_order(folder),
        "created": _now_iso(),
        "updated": _now_iso(),
    })
    full_body = _join_sections(body, solution, [])
    target_dir = _folder_abspath(folder)
    path = target_dir / f"{qid}.md"
    with _write_lock:
        _write_raw(path, meta, full_body)
    return qid


def update_question(qid: str, body: str, solution: str = "", qtype: str = "",
                     source: str = "", difficulty: str = "",
                     tags: list[str] | None = None):
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
    full_body = _join_sections(body, solution, rec["extra_sections"])
    with _write_lock:
        _write_raw(path, meta, full_body)


def _update_meta_fields(qid: str, fields: dict):
    """就地更新某题 frontmatter 里的若干字段，正文不变。"""
    rec = get_question(qid)
    if not rec:
        return
    path = config.BANK_DIR / rec["path"]
    meta, body = _read_raw(path)
    meta.update(fields)
    meta["updated"] = _now_iso()
    _write_raw(path, meta, body)


def delete_question(qid: str):
    rec = get_question(qid)
    if not rec:
        return
    src = config.BANK_DIR / rec["path"]
    trash_name = f"{src.stem}__{_new_id()}{src.suffix}"
    dst = config.TRASH_DIR / trash_name
    meta, body = _read_raw(src)
    meta["_trash_original_path"] = rec["path"]
    meta["_trash_deleted_at"] = _now_iso()
    with _write_lock:
        _write_raw(src, meta, body)  # 先落盘记录原路径
        shutil.move(str(src), str(dst))
    _selected.discard(qid)


def list_deleted_questions() -> list[dict]:
    out = []
    if not config.TRASH_DIR.exists():
        return out
    for path in config.TRASH_DIR.glob("*.md"):
        try:
            meta, body = _read_raw(path)
        except Exception:
            continue
        rec = _to_record(path, meta, body)
        rec["original_path"] = meta.get("_trash_original_path", "")
        rec["deleted_at"] = meta.get("_trash_deleted_at", "")
        out.append(rec)
    out.sort(key=lambda r: r["deleted_at"], reverse=True)
    return out


def restore_question(qid: str):
    for path in config.TRASH_DIR.glob("*.md"):
        meta, body = _read_raw(path)
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
        return
    raise KeyError(qid)


def purge_question(qid: str):
    for path in config.TRASH_DIR.glob("*.md"):
        meta, _ = _read_raw(path)
        if str(meta.get("id")) == str(qid):
            path.unlink()
            return


def empty_recycle_bin():
    if not config.TRASH_DIR.exists():
        return
    for entry in config.TRASH_DIR.iterdir():
        if entry.name == "_assets":
            continue
        if entry.is_dir():
            shutil.rmtree(entry)
        else:
            entry.unlink()


# ---------------------------------------------------------------------------
# 单字段更新 / 图片布局 / 星标 / 勾选
# ---------------------------------------------------------------------------


def set_difficulty(qid: str, difficulty: str):
    _update_meta_fields(qid, {"difficulty": difficulty})


def set_type(qid: str, qtype: str):
    _update_meta_fields(qid, {"type": qtype})


def set_img_align(qid: str, align: str | None):
    """设置首图（index 0）的水平位置：left/center/right，空清除。"""
    _update_meta_fields(qid, {"img_align": align or ""})


def set_img_width(qid: str, width):
    """设置首图（index 0）的宽度百分比（10-100），空/None 清除。"""
    _update_meta_fields(qid, {"img_width": int(width) if width not in (None, "") else None})


def set_img_split(qid: str, mode: str | None):
    """图文分栏模式：''/opts/full/sub。"""
    if mode in ("opts", "full", "sub"):
        val = mode
    elif mode:
        val = "opts"
    else:
        val = ""
    _update_meta_fields(qid, {"img_split": val})


def set_img_layout(qid: str, index: int, align=None, width=None):
    """设置第 index 张图（0 起）的宽度/对齐，落进 img_layouts JSON 列表。

    align/width 传 None 表示"本次不动这一项"，传 "" 表示"清除该项"。
    index==0 时一并回写标量 img_align/img_width（供无 img_layouts 的旧路径读取）。
    """
    rec = get_question(qid)
    if not rec:
        return
    items = [dict(it) for it in rec["img_layouts"] if isinstance(it, dict)]
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

    items = [it for it in items if it.get("w") is not None or it.get("align")]
    items.sort(key=lambda it: it.get("i", 0))

    fields = {"img_layouts": items}
    if index == 0:
        if width is not None:
            fields["img_width"] = int(width) if width != "" else None
        if align is not None:
            fields["img_align"] = align or ""
    _update_meta_fields(qid, fields)


def toggle_starred(qid: str):
    rec = get_question(qid)
    if rec:
        _update_meta_fields(qid, {"starred": not rec["starred"]})


def set_starred_many(ids: list[str], starred: bool):
    for qid in ids:
        _update_meta_fields(qid, {"starred": starred})


def toggle_selected(qid: str) -> bool:
    if qid in _selected:
        _selected.discard(qid)
        return False
    _selected.add(qid)
    return True


def clear_selected():
    _selected.clear()


def select_ids(ids: list[str]):
    _selected.update(ids)


def select_all(ids: list[str]):
    _selected.update(ids)


def count_selected() -> int:
    return len(_selected)


def reorder(ids: list[str]):
    for i, qid in enumerate(ids):
        _update_meta_fields(qid, {"order": float(i)})


# ---------------------------------------------------------------------------
# 题目所属文件夹（题目所在目录即其"所属文件夹"，移动=移动文件）
# ---------------------------------------------------------------------------


def collections_of(qid: str) -> list[str]:
    rec = get_question(qid)
    return [rec["folder"]] if rec and rec["folder"] else []


def add_to_collection(qid: str, folder_id: str):
    """把题目移动到指定文件夹（一题只能在一个目录下，与目录语义一致）。"""
    rec = get_question(qid)
    if not rec:
        return
    src = config.BANK_DIR / rec["path"]
    dst_dir = _folder_abspath(folder_id)
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / src.name
    if src == dst:
        return
    if dst.exists():
        dst = dst_dir / f"{src.stem}_{_new_id()}{src.suffix}"
    with _write_lock:
        shutil.move(str(src), str(dst))


def remove_from_collection(qid: str, folder_id: str = ""):
    """从文件夹移出 = 移到题库根目录。"""
    rec = get_question(qid)
    if not rec or rec["folder"] != folder_id:
        return
    add_to_collection(qid, "")


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
# 列表查询：过滤 / 排序（Python 侧内存过滤，取代 db.py 的 SQL）
# ---------------------------------------------------------------------------

_SORT_KEYS = {
    "custom": lambda r: r["order"],
    "created_desc": lambda r: r["created"],
    "created_asc": lambda r: r["created"],
    "difficulty": lambda r: (r["difficulty"] or "0"),
    "type": lambda r: r["type"],
    "starred": lambda r: (0 if r["starred"] else 1, r["order"]),
}
_SORT_REVERSE = {"created_desc": True}


def _folder_subtree_ids(folder_id: str) -> set[str]:
    ids = {folder_id}
    for f in all_collections():
        if f["id"] == folder_id or f["id"].startswith(folder_id + "/"):
            ids.add(f["id"])
    return ids


def list_questions(tags: list[str] | None = None, match: str = "and",
                    qtype: str = "", difficulty: str = "",
                    starred: bool = False, sort: str = "custom",
                    collection: str = "", search: str = "",
                    selected_only: bool = False) -> list[dict]:
    recs = _all_records()

    if collection:
        subtree = _folder_subtree_ids(collection)
        recs = [r for r in recs if r["folder"] in subtree]
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
        needle = search.strip().lower()
        if needle:
            recs = [
                r for r in recs
                if needle in r["body"].lower()
                or needle in r["solution"].lower()
                or any(needle in t.lower() for t in r["tags"])
            ]

    key = _SORT_KEYS.get(sort, _SORT_KEYS["custom"])
    recs = sorted(recs, key=key, reverse=_SORT_REVERSE.get(sort, False))
    return recs
