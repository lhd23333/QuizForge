"""QuizForge 软件版 —— 单机文件式题库 Web 应用。

安全说明：仅监听 127.0.0.1，无鉴权，供本地单人使用。请勿暴露到公网。

运行：
    python app.py
浏览器打开 http://127.0.0.1:5000
"""

from flask import (Flask, render_template, request, redirect, url_for,
                   jsonify, flash, send_file, abort)
from markupsafe import Markup, escape
from werkzeug.utils import safe_join

import config
import filestore
import importer
import exporter
import dedup
import converter
import crypto_utils
import llm_client
import providers

import re
import uuid
import threading
from pathlib import Path

app = Flask(__name__)
app.secret_key = "quizbank-local-dev"  # 本地会话用，非安全敏感

filestore.init_store()


# 题目正文里的图片引用：Obsidian 双链嵌入 ![[filename]]（可带 |alt 后缀）——
# 由 converter/save_image 写入，filename 只在 config.ASSETS_DIR 下找。
_QIMG_RE = re.compile(r"!\[\[([^\]\|]+)(?:\|[^\]]*)?\]\]")


@app.template_filter("qimage")
def qimage_filter(text):
    """把题目正文安全地渲染为 HTML：先整体转义（保护数学式里的 < >、& 等），
    再把其中的 Obsidian 图片嵌入 `![[filename]]` 替换成真正的 <img> 标签。

    顺序很关键：必须先转义再替换，否则 body 里的 `$a<b$` 会被当成 HTML 标签；
    filename 由 converter/上传流程生成、可信，故替换出的 <img> 用 Markup 放行。
    """
    if not text:
        return ""
    escaped = str(escape(text))  # 数学式里的 < 变成 &lt;，安全

    def _to_img(m):
        filename = m.group(1)
        src = url_for("asset_serve", filename=filename)
        return (f'<img src="{src}" alt="" '
                f'style="max-width:100%;height:auto;display:block;margin:6px 0;">')

    # escape 后 ![[]] 的括号/叹号不受影响，仍可匹配
    return Markup(_QIMG_RE.sub(_to_img, escaped))


@app.context_processor
def _inject_types():
    """题型列表全模板可用。校对页的逐题题型下拉要用它，而校对页有三个入口
    （单文件/md 队列/方式四看板）都渲染 import.html，逐个传参容易漏。"""
    return {"types": config.QUESTION_TYPES}


# ---------------------------------------------------------------------------
# 题目列表 + 筛选
# ---------------------------------------------------------------------------


@app.route("/")
def index():
    # 兼容两种传参：checkbox 多选 ?tag=x&tag=y，或逗号串 ?tags=x,y
    tags = request.args.getlist("tag")
    if not tags:
        tags = [t for t in request.args.get("tags", "").split(",") if t.strip()]
    tags = [t for t in tags if t.strip()]
    match = request.args.get("match", "and")
    type_ = request.args.get("type") or None
    difficulty = request.args.get("difficulty") or None
    starred_only = request.args.get("starred") in ("1", "true", "on")
    sort = request.args.get("sort", "custom")
    collection_id = request.args.get("collection") or ""
    search = request.args.get("q", "").strip()

    questions = filestore.list_questions(
        tags=tags, match=match, qtype=type_ or "", sort=sort,
        collection=collection_id, search=search,
        difficulty=difficulty or "", starred=starred_only)
    all_tags = filestore.all_tags()
    all_cols = filestore.all_collections()
    folder_tree = filestore.list_collections_tree()
    cur_col = filestore.get_collection(collection_id) if collection_id else None
    selected_count = filestore.count_selected()

    return render_template(
        "index.html",
        questions=questions,
        all_tags=all_tags,
        active_tags=tags,
        match=match,
        active_type=type_,
        types=config.QUESTION_TYPES,
        active_difficulty=difficulty,
        difficulties=config.DIFFICULTIES,
        starred_only=starred_only,
        selected_count=selected_count,
        sort=sort,
        all_collections=all_cols,
        folder_tree=folder_tree,
        active_collection=collection_id,
        active_collection_name=(cur_col["name"] if cur_col else None),
        search=search,
    )


# ---------------------------------------------------------------------------
# 单题 新增 / 编辑 / 删除
# ---------------------------------------------------------------------------


@app.route("/question/new", methods=["GET", "POST"])
def question_new():
    if request.method == "POST":
        _save_from_form()
        flash("题目已新增", "ok")
        return redirect(url_for("index"))
    return render_template("edit.html", q=None, q_tags=[],
                           types=config.QUESTION_TYPES,
                           difficulties=config.DIFFICULTIES)


@app.route("/question/<qid>/edit", methods=["GET", "POST"])
def question_edit(qid):
    q = filestore.get_question(qid)
    if not q:
        abort(404)
    if request.method == "POST":
        _save_from_form(qid)
        flash("题目已更新", "ok")
        return redirect(url_for("index"))
    q_tags = q["tags"]
    return render_template("edit.html", q=q, q_tags=q_tags,
                           types=config.QUESTION_TYPES,
                           difficulties=config.DIFFICULTIES)


@app.route("/question/<qid>/delete", methods=["POST"])
def question_delete(qid):
    filestore.delete_question(qid)
    flash("题目已删除", "ok")
    return redirect(request.referrer or url_for("index"))


def _save_from_form(qid=None):
    """从表单读字段，新增或更新。"""
    body = request.form.get("body", "").strip()
    solution = request.form.get("solution", "").strip()
    type_ = request.form.get("type") or ""
    source = request.form.get("source", "").strip()
    difficulty = request.form.get("difficulty") or ""
    tag_names = [t.strip() for t in request.form.get("tags", "").split(",") if t.strip()]
    if qid is None:
        filestore.create_question(body, solution=solution, qtype=type_,
                                  source=source, difficulty=difficulty,
                                  tags=tag_names)
    else:
        filestore.update_question(qid, body, solution=solution, qtype=type_,
                                  source=source, difficulty=difficulty,
                                  tags=tag_names)


# ---------------------------------------------------------------------------
# 勾选组卷
# ---------------------------------------------------------------------------


@app.route("/question/<qid>/toggle", methods=["POST"])
def question_toggle(qid):
    new_val = filestore.toggle_selected(qid)
    count = filestore.count_selected()
    return jsonify(selected=new_val, count=count)


@app.route("/question/<qid>/difficulty", methods=["POST"])
def question_difficulty(qid):
    """设置难度（AJAX）。level 为 '1'~'5' 或 '' 清除。"""
    data = request.get_json(silent=True) or {}
    level = str(data.get("level", "")).strip()
    if level and level not in ("1", "2", "3", "4", "5"):
        return jsonify(ok=False, error="难度须为 1-5"), 400
    filestore.set_difficulty(qid, level)
    return jsonify(ok=True, level=level)


@app.route("/question/<qid>/star", methods=["POST"])
def question_star(qid):
    """切换标星（AJAX），返回新状态。"""
    filestore.toggle_starred(qid)
    q = filestore.get_question(qid)
    return jsonify(ok=True, starred=bool(q and q["starred"]))


@app.route("/question/<qid>/type", methods=["POST"])
def question_type_set(qid):
    """设置题型（AJAX）。"""
    data = request.get_json(silent=True) or {}
    type_ = str(data.get("type", "")).strip()
    if type_ and type_ not in config.QUESTION_TYPES:
        return jsonify(ok=False, error="未知题型"), 400
    filestore.set_type(qid, type_)
    return jsonify(ok=True, type=type_)


# 单题最多可逐图设置的图片数。上限只为挡住 img_layouts 被撑成超大 JSON，
# 正常题目远达不到。
_MAX_IMG_INDEX = 49


def _img_index(data):
    """从请求体取图片序号（0 起）。缺省=0（兼容只发首图设置的旧请求）。
    非法/越界返回 None，由调用方回 400。"""
    raw = data.get("index", 0)
    try:
        idx = int(raw)
    except (TypeError, ValueError):
        return None
    return idx if 0 <= idx <= _MAX_IMG_INDEX else None


@app.route("/question/<qid>/img_align", methods=["POST"])
def question_img_align(qid):
    """设置某张图的水平位置（AJAX）：left/center/right 或空清除。

    index = 正文里图片出现的序号（0 起，缺省 0）；落进 img_layouts，index==0 时
    filestore.set_img_layout 一并回写旧的 img_align 字段。
    """
    data = request.get_json(silent=True) or {}
    align = str(data.get("align", "")).strip()
    if align and align not in ("left", "center", "right"):
        return jsonify(ok=False, error="非法位置"), 400
    index = _img_index(data)
    if index is None:
        return jsonify(ok=False, error="非法图片序号"), 400
    filestore.set_img_layout(qid, index, align=align)
    return jsonify(ok=True, align=align, index=index)


@app.route("/question/<qid>/img_width", methods=["POST"])
def question_img_width(qid):
    """设置某张图的宽度百分比（AJAX）：10-100 或空清除（走默认宽度）。

    index 语义同 question_img_align。
    """
    data = request.get_json(silent=True) or {}
    width = data.get("width")
    if width in (None, ""):
        width = ""          # "" = 清除该项（区别于 None「不动」，见 filestore.set_img_layout）
    else:
        try:
            width = int(width)
        except (TypeError, ValueError):
            return jsonify(ok=False, error="宽度须为整数"), 400
        if not (10 <= width <= 100):
            return jsonify(ok=False, error="宽度须在 10-100 之间"), 400
    index = _img_index(data)
    if index is None:
        return jsonify(ok=False, error="非法图片序号"), 400
    filestore.set_img_layout(qid, index, width=width)
    return jsonify(ok=True, width=width if width != "" else None, index=index)


@app.route("/question/<qid>/img_split", methods=["POST"])
def question_img_split(qid):
    """设置图文分栏模式（AJAX）：''/opts/full/sub。"""
    data = request.get_json(silent=True) or {}
    mode = str(data.get("mode", "")).strip()
    if mode and mode not in ("opts", "full", "sub"):
        return jsonify(ok=False, error="非法模式"), 400
    filestore.set_img_split(qid, mode)
    return jsonify(ok=True, mode=mode)


@app.route("/clear", methods=["POST"])
def clear():
    filestore.clear_selected()
    flash("已清空所有勾选", "ok")
    return redirect(request.referrer or url_for("index"))


@app.route("/delete_selected", methods=["POST"])
def delete_selected():
    """删除当前已勾选的题目（破坏性，移入回收站）。"""
    ids = [r["id"] for r in filestore.list_questions(selected_only=True)]
    for qid in ids:
        filestore.delete_question(qid)
    if ids:
        flash(f"已删除 {len(ids)} 道题", "ok")
    else:
        flash("没有勾选任何题目", "err")
    return redirect(request.referrer or url_for("index"))


@app.route("/select_all", methods=["POST"])
def select_all():
    """全选当前筛选结果（按界面上所有生效的筛选条件查出题目，批量勾选）。"""
    tags = [t for t in request.form.get("tags", "").split(",") if t.strip()]
    match = request.form.get("match", "and")
    type_ = request.form.get("type") or ""
    difficulty = request.form.get("difficulty") or ""
    search = request.form.get("q", "").strip()
    starred_only = request.form.get("starred") in ("1", "true", "on")
    collection_id = request.form.get("collection", "")
    rows = filestore.list_questions(tags=tags, match=match, qtype=type_,
                                    difficulty=difficulty, search=search,
                                    starred=starred_only,
                                    collection=collection_id)
    filestore.select_ids([r["id"] for r in rows])
    flash(f"已全选 {len(rows)} 道题", "ok")
    return redirect(request.referrer or url_for("index"))


@app.route("/tags/<name>/rename", methods=["POST"])
def tag_rename(name):
    """标签改名（新名已存在则合并）。name 是旧标签名本身。"""
    new_name = request.form.get("name", "").strip()
    if not new_name:
        flash("新标签名不能为空", "err")
        return redirect(request.referrer or url_for("index"))
    filestore.rename_tag(name, new_name)
    flash(f"标签已改名为「{new_name}」" if name != new_name else "未改动", "ok")
    return redirect(request.referrer or url_for("index"))


@app.route("/tag_selected", methods=["POST"])
def tag_selected():
    """给当前已勾选的题批量追加标签。"""
    tag_names = [t.strip() for t in request.form.get("tags", "").split(",") if t.strip()]
    if not tag_names:
        flash("请填写要添加的标签", "err")
        return redirect(request.referrer or url_for("index"))
    ids = [r["id"] for r in filestore.list_questions(selected_only=True)]
    if not ids:
        flash("请先勾选题目", "err")
    else:
        filestore.add_tags_to(ids, tag_names)
        flash(f"已给 {len(ids)} 道题添加标签：{'、'.join(tag_names)}", "ok")
    return redirect(request.referrer or url_for("index"))


@app.route("/difficulty_selected", methods=["POST"])
def difficulty_selected():
    """给当前已勾选的题批量设置难度（level 为 1-5 或 '' 清除）。"""
    level = request.form.get("level", "").strip()
    if level and level not in ("1", "2", "3", "4", "5"):
        flash("难度须为 1-5", "err")
        return redirect(request.referrer or url_for("index"))
    ids = [r["id"] for r in filestore.list_questions(selected_only=True)]
    if not ids:
        flash("请先勾选题目", "err")
    else:
        for qid in ids:
            filestore.set_difficulty(qid, level)
        label = f"难度 {level}" if level else "清除难度"
        flash(f"已给 {len(ids)} 道题设置：{label}", "ok")
    return redirect(request.referrer or url_for("index"))


# ---------------------------------------------------------------------------
# 题集（= 文件夹）
# ---------------------------------------------------------------------------


def _selected_ids() -> list[str]:
    """当前已勾选的题 id 列表。"""
    return [r["id"] for r in filestore.list_questions(selected_only=True)]


@app.route("/collections/create", methods=["POST"])
def collection_create():
    name = request.form.get("name", "").strip()
    parent_id = request.form.get("parent_id", "")
    if not name:
        flash("题集名不能为空", "err")
    else:
        try:
            filestore.create_collection(name, parent_id=parent_id)
            flash(f"已新建题集「{name}」", "ok")
        except ValueError as e:
            flash(str(e), "err")
    return redirect(request.referrer or url_for("index"))


@app.route("/collections/<path:cid>/rename", methods=["POST"])
def collection_rename(cid):
    new_name = request.form.get("name", "").strip()
    if not new_name:
        flash("题集名不能为空", "err")
    else:
        try:
            filestore.rename_collection(cid, new_name)
            flash(f"已改名为「{new_name}」", "ok")
        except ValueError as e:
            flash(str(e), "err")
    return redirect(request.referrer or url_for("index"))


@app.route("/collections/<path:cid>/move", methods=["POST"])
def collection_move(cid):
    """把文件夹 cid 移到新的父级下（AJAX），parent_id 为空表示移到顶级。"""
    data = request.get_json(silent=True) or {}
    new_parent = data.get("parent_id") or ""
    try:
        filestore.move_folder(cid, new_parent)
        return jsonify(ok=True)
    except ValueError as e:
        return jsonify(ok=False, error=str(e))


@app.route("/collections/<path:cid>/delete", methods=["POST"])
def collection_delete(cid):
    filestore.delete_collection(cid)
    flash("已移入回收站", "ok")
    # 若正停留在被删题集视图，回到全部
    return redirect(url_for("index"))


@app.route("/collections/<path:cid>/add", methods=["POST"])
def collection_add(cid):
    """把当前已勾选的题加入题集。"""
    ids = _selected_ids()
    if not ids:
        flash("请先勾选题目再加入题集", "err")
    else:
        col = filestore.get_collection(cid)
        for qid in ids:
            filestore.add_to_collection(qid, cid)
        flash(f"已把 {len(ids)} 道题加入「{col['name'] if col else ''}」", "ok")
    return redirect(request.referrer or url_for("index"))


@app.route("/collections/<path:cid>/remove", methods=["POST"])
def collection_remove(cid):
    """把当前已勾选的题移出题集（题目本身保留）。"""
    ids = _selected_ids()
    if not ids:
        flash("请先勾选要移出的题目", "err")
    else:
        for qid in ids:
            filestore.remove_from_collection(qid, cid)
        flash(f"已把 {len(ids)} 道题移出本集", "ok")
    return redirect(request.referrer or url_for("index"))


@app.route("/reorder", methods=["POST"])
def reorder():
    """拖拽排序：接收 {"ids": ["a1b2...", ...]} 落盘为 order。"""
    data = request.get_json(silent=True) or {}
    ids = [str(x) for x in data.get("ids", [])]
    if not ids:
        return jsonify(ok=False, error="empty"), 400
    filestore.reorder(ids)
    return jsonify(ok=True, count=len(ids))


# ---------------------------------------------------------------------------
# 上传文件 → 后台转换（MinerU+DeepSeek）→ 规范化 md
# ---------------------------------------------------------------------------

# 内存任务表：job_id -> {status: pending|done|error, md, error, filename}
# 本地单用户，转换串行，用简单字典 + 锁即可，无需 Celery。
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()

# 方式四「多组 PDF 批量导入」内存表：batch_id -> {
#   status: converting|done|error,
#   groups: [ {job_id, file_path, solution_path|None, include_solution,
#              only_numbers|None, filename, status, md|None, error|None} ],
#   current_idx: int,            # 当前转到第几组（供前端进度显示）
#   batch_queue_id: str|None,    # 转完后填，前端据此跳转逐组校对队列
# }
# 每组的 job_id 同时在 _jobs 里登记（含 path），从而 convert_file_view 与校对页
# 左侧 PDF 对照 iframe 零改动复用。内存态，关服务即丢。
_batch_jobs: dict[str, dict] = {}
_batch_jobs_lock = threading.Lock()


def _parse_number_spec(spec: str):
    """把题号规格串解析成整数列表；空串/无效返回 None（=全部，不过滤）。

    支持逗号分隔与区间：'8,11,14,18,19' 或 '7-14,18'（区间含两端）。
    去重、升序。上限保护：单个题号 <= 999，避免异常输入。
    """
    if not spec or not spec.strip():
        return None
    nums = set()
    for part in spec.replace("，", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, _, b = part.partition("-")
            try:
                lo, hi = int(a), int(b)
            except ValueError:
                continue
            if lo > hi:
                lo, hi = hi, lo
            for n in range(lo, hi + 1):
                if 1 <= n <= 999:
                    nums.add(n)
        else:
            try:
                n = int(part)
            except ValueError:
                continue
            if 1 <= n <= 999:
                nums.add(n)
    return sorted(nums) or None


def _parse_engine(raw: str) -> str:
    """表单里的识别引擎选择。只认显式的 "block"，其余（含缺省）一律走老路径——
    新路径是加出来的第二条路，不能因为参数名写错就把默认行为换掉。
    """
    return (converter.ENGINE_BLOCK if (raw or "").strip() == "block"
            else converter.ENGINE_WHOLE)


def _convert_worker(job_id: str, saved_path, orig_filename: str,
                    include_solution: bool = False, solution_path=None,
                    only_numbers=None, provider=None,
                    engine: str = converter.ENGINE_WHOLE):
    """后台线程：跑转换，结果写回 _jobs。

    solution_path 非空 → 走「题干+解析双文件」路径，按题号关联解析。
    only_numbers 非空 → 仅导入指定题号的题（压轴题过滤）。
    provider 在起线程前就解析好传进来（这里没有请求上下文）。
    上传文件不在此删除——保留供预览对照，由下次转换前 _clean_uploads 清理。
    """
    try:
        if solution_path is not None:
            md = converter.convert_exam_and_solution(
                saved_path, solution_path, only_numbers=only_numbers,
                provider=provider, engine=engine)
        else:
            md = converter.convert_file(
                saved_path, include_solution=include_solution,
                only_numbers=only_numbers, provider=provider, engine=engine)
        with _jobs_lock:
            _jobs[job_id].update(status="done", md=md)
    except Exception as e:
        with _jobs_lock:
            _jobs[job_id].update(status="error", error=str(e))


def _clean_uploads(keep_path=None):
    """清理 uploads 里的旧文件（保留 keep_path）。仿 exporter._clean_output。

    只 unlink 文件；对 batch/ 子目录 unlink 会抛 OSError 被跳过，故方式四
    的在途文件（存于 uploads/batch/）天然不受此清理影响。
    """
    if not config.UPLOAD_DIR.exists():
        return
    for f in config.UPLOAD_DIR.iterdir():
        if keep_path and f.resolve() == keep_path.resolve():
            continue
        try:
            f.unlink()
        except OSError:
            pass  # 被占用（如正在预览）或为目录就跳过


def _convert_batch_worker(batch_id: str):
    """后台线程：对 _batch_jobs[batch_id] 的各组串行转换。

    一次只跑一个 converter，逐组按参数调 convert_file / convert_exam_and_solution
    （与 _convert_worker 一致）。每组结果同步写回该组在 _jobs 里的登记，供校对
    页 PDF 对照。某组抛异常只标记该组 error，不中断整批。全部转完后建校对队列。
    """
    with _batch_jobs_lock:
        batch = _batch_jobs.get(batch_id)
        if not batch:
            return
        batch["status"] = "converting"
        groups = batch["groups"]

    for i, g in enumerate(groups):
        with _batch_jobs_lock:
            batch["current_idx"] = i
            g["status"] = "converting"
        job_id = g["job_id"]
        with _jobs_lock:
            if job_id in _jobs:
                _jobs[job_id]["status"] = "converting"
        # 每组转换前现查一次：整批可能跑很久，中途在设置页换了模型应当生效
        provider = providers.resolve_active()
        engine = g.get("engine") or converter.ENGINE_WHOLE
        try:
            if g["solution_path"] is not None:
                md = converter.convert_exam_and_solution(
                    g["file_path"], g["solution_path"],
                    only_numbers=g["only_numbers"],
                    provider=provider, engine=engine)
            else:
                md = converter.convert_file(
                    g["file_path"], include_solution=g["include_solution"],
                    only_numbers=g["only_numbers"],
                    provider=provider, engine=engine)
            with _batch_jobs_lock:
                g["md"] = md
                g["status"] = "done"
            with _jobs_lock:
                if job_id in _jobs:
                    _jobs[job_id].update(status="done", md=md)
        except Exception as e:
            with _batch_jobs_lock:
                g["error"] = str(e)
                g["status"] = "error"
            with _jobs_lock:
                if job_id in _jobs:
                    _jobs[job_id].update(status="error", error=str(e))

    with _batch_jobs_lock:
        conv_done = all(g["status"] in ("done", "error") for g in groups)
        batch["status"] = "done" if conv_done else "converting"
    _maybe_finish_batch(batch_id)


def _group_terminal(g) -> bool:
    """该组是否已到最终态：转换失败，或已审核（导入/跳过）。"""
    return g["status"] == "error" or g.get("reviewed") in ("imported", "skipped")


def _group_files(g) -> list[str]:
    """该组需清理的全部落盘文件路径（去重）。

    多图组存 cleanup_paths（含原始图片 + 合成 PDF）；老结构/单文件组
    退回 file_path/solution_path，向后兼容。
    """
    paths = list(g.get("cleanup_paths") or [])
    for key in ("file_path", "solution_path"):
        p = g.get(key)
        if p and p not in paths:
            paths.append(p)
    return paths


def _maybe_finish_batch(batch_id: str):
    """若整批转换结束且各组都到最终态，清该批上传文件与 _jobs 条目（保留
    _batch_jobs 记录，使看板仍可显示汇总）。幂等：已清过则跳过。"""
    with _batch_jobs_lock:
        batch = _batch_jobs.get(batch_id)
        if not batch or batch.get("files_cleaned"):
            return
        conv_done = batch["status"] in ("done", "error")
        if not (conv_done and all(_group_terminal(g) for g in batch["groups"])):
            return
        batch["files_cleaned"] = True
        groups = list(batch["groups"])
    for g in groups:
        for p in _group_files(g):
            try:
                Path(p).unlink()
            except OSError:
                pass
        with _jobs_lock:
            _jobs.pop(g["job_id"], None)


def _clean_batch_uploads(batch_id: str):
    """取消整批时：删该批所有上传文件、释放 _batch_jobs 与 _jobs 条目。"""
    with _batch_jobs_lock:
        batch = _batch_jobs.pop(batch_id, None)
    if not batch:
        return
    for g in batch["groups"]:
        for p in _group_files(g):
            try:
                Path(p).unlink()
            except OSError:
                pass
        with _jobs_lock:
            _jobs.pop(g["job_id"], None)


@app.route("/convert/start", methods=["POST"])
def convert_start():
    """接收上传文件，起后台转换线程，返回 job_id 和原始文件名。"""
    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify(ok=False, error="未选择文件"), 400
    orig_filename = file.filename
    # 是否同时识别解析（较慢）：前端勾选框传入
    include_solution = request.form.get("include_solution") in ("1", "true", "on")
    # 可选：只导入指定题号（压轴题过滤），如 "8,11,14,18,19" 或含区间 "7-14,18"
    only_numbers = _parse_number_spec(request.form.get("only_numbers", ""))
    # 识别引擎：整篇规范化（默认）/ 逐块识别
    engine = _parse_engine(request.form.get("engine", ""))
    # 设置页里启用的 LLM 配置，在请求线程里解析好再传给后台线程
    provider = providers.resolve_active()
    # 可选：单独的解析/答案文件（题干与解析分属两个文件时）
    sol_file = request.files.get("solution_file")

    config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    _clean_uploads()   # 清掉上一次遗留的上传文件

    def _save(f, jid):
        """按 uuid 名存盘，避免中文/特殊字符问题；返回落盘路径。"""
        name = f.filename or ""
        ext = "".join(c for c in name[name.rfind("."):] if c not in '/\\') if "." in name else ""
        p = config.UPLOAD_DIR / f"{jid}{ext}"
        f.save(str(p))
        return p

    job_id = uuid.uuid4().hex
    saved_path = _save(file, job_id)

    # 解析文件存在则走双文件路径（用另一个 uuid 名，避免覆盖题干文件）
    solution_path = None
    if sol_file and sol_file.filename:
        solution_path = _save(sol_file, uuid.uuid4().hex)

    with _jobs_lock:
        _jobs[job_id] = {"status": "pending", "md": None, "error": None,
                         "filename": orig_filename, "path": str(saved_path)}
    threading.Thread(target=_convert_worker,
                     args=(job_id, saved_path, orig_filename, include_solution,
                           solution_path, only_numbers, provider, engine),
                     daemon=True).start()
    return jsonify(ok=True, job_id=job_id, filename=orig_filename)


@app.route("/convert/file/<job_id>")
def convert_file_view(job_id):
    """返回上传的原文件（供预览 iframe/img 显示）。

    直接用登记时存下的 path（服务端 uuid 命名、可信）。方式一存 UPLOAD_DIR，
    方式四存 UPLOAD_DIR/batch/——都在 UPLOAD_DIR 之下。校验落在该目录内防穿越。
    """
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job or not job.get("path"):
        abort(404)
    path = Path(job["path"]).resolve()
    upload_root = config.UPLOAD_DIR.resolve()
    if upload_root not in path.parents or not path.is_file():
        abort(404)
    return send_file(str(path), as_attachment=False)


@app.route("/assets/<path:filename>")
def asset_serve(filename):
    """伺服题目插图（存于 config.ASSETS_DIR 的扁平资产目录）。

    safe_join 防目录穿越，越界返回 404。
    """
    full = safe_join(str(config.ASSETS_DIR), filename)
    if full is None or not Path(full).is_file():
        abort(404)
    return send_file(full)


@app.route("/convert/status/<job_id>")
def convert_status(job_id):
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        return jsonify(status="error", error="任务不存在"), 404
    return jsonify(status=job["status"], md=job.get("md"),
                   error=job.get("error"), filename=job.get("filename"))


# --- 方式四：多组 PDF 批量导入 -------------------------------------------

_MAX_BATCH_GROUPS = 20   # 单批任务组数上限，防异常输入


@app.route("/batch-convert/create", methods=["POST"])
def batch_convert_create():
    """接收多组文件配置，逐组存盘（uploads/batch/）、登记 _jobs 与 _batch_jobs，
    起串行转换线程。前端以 groups[i][file] / [solution_file] / [include_solution]
    / [only_numbers] 形式提交。返回 {ok, batch_id, count}。"""
    # 探测组数：收集所有 groups[i][file] 的 i（每组题干/解析都可多文件）
    idxs = set()
    for key in request.files:
        m = re.match(r"groups\[(\d+)\]\[file\]$", key)
        if m:
            idxs.add(int(m.group(1)))
    if not idxs:
        return jsonify(ok=False, error="未添加任何任务组"), 400
    if len(idxs) > _MAX_BATCH_GROUPS:
        return jsonify(ok=False, error=f"任务组过多（上限 {_MAX_BATCH_GROUPS}）"), 400

    config.BATCH_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

    def _save(f):
        """按 uuid 名存进 batch 子目录；返回落盘路径字符串。"""
        name = f.filename or ""
        ext = "".join(c for c in name[name.rfind("."):] if c not in '/\\') if "." in name else ""
        p = config.BATCH_UPLOAD_DIR / f"{uuid.uuid4().hex}{ext}"
        f.save(str(p))
        return str(p)

    def _save_many(files):
        """存一组文件（题干或解析），返回落盘路径列表（保序，过滤空项）。"""
        return [_save(f) for f in files if f and f.filename]

    def _resolve_input(paths):
        """把一组已落盘的文件解析成单个待转换文件路径：
        - 空 → None（无此文件）；
        - 单个 → 原样返回（PDF/Word/单图走各自原有分支）；
        - 多个 → 若全为图片，按序合成一个 PDF（复用 PDF 转换链路）；
          含非图片的多文件不支持合成，取第一个并告警（避免静默丢文件）。
        """
        if not paths:
            return None
        if len(paths) == 1:
            return paths[0]
        if all(converter.is_image_file(p) for p in paths):
            merged = config.BATCH_UPLOAD_DIR / f"{uuid.uuid4().hex}.pdf"
            converter.images_to_pdf(paths, merged)
            return str(merged)
        return paths[0]

    groups = []
    for i in sorted(idxs):
        file_list = _save_many(request.files.getlist(f"groups[{i}][file]"))
        if not file_list:
            continue
        sol_list = _save_many(request.files.getlist(f"groups[{i}][solution_file]"))
        include_solution = request.form.get(f"groups[{i}][include_solution]") in ("1", "true", "on")
        only_numbers = _parse_number_spec(request.form.get(f"groups[{i}][only_numbers]", ""))
        # 引擎可整批统一给（表单顶层 engine），也可每组单独给，组内优先
        engine = _parse_engine(request.form.get(f"groups[{i}][engine]", "")
                               or request.form.get("engine", ""))
        job_id = uuid.uuid4().hex
        # 多图合成 PDF；单文件原样。原始上传件也留着，整批结束时统一清理。
        file_path = _resolve_input(file_list)
        solution_path = _resolve_input(sol_list)
        # 选了解析文件即视为带解析（与方式一一致）
        if solution_path is not None:
            include_solution = True
        first = request.files.getlist(f"groups[{i}][file]")[0]
        with _jobs_lock:
            _jobs[job_id] = {"status": "pending", "md": None, "error": None,
                             "filename": first.filename, "path": file_path}
        # 显示名：单文件用原名；多文件合成时标「原名 等 N 张」
        first_name = first.filename
        n_files = len(file_list)
        disp_name = first_name if n_files == 1 else f"{first_name} 等 {n_files} 张"
        # 所有落盘文件（原始上传 + 合成 PDF）都要在清理时删除，避免残留
        extra = list(file_list) + list(sol_list)
        if file_path and file_path not in extra:
            extra.append(file_path)
        if solution_path and solution_path not in extra:
            extra.append(solution_path)
        groups.append({
            "gid": i, "job_id": job_id, "file_path": file_path,
            "solution_path": solution_path, "include_solution": include_solution,
            "only_numbers": only_numbers, "filename": disp_name,
            "engine": engine,
            "cleanup_paths": extra,   # 需清理的全部落盘文件（含原图与合成 PDF）
            "status": "pending", "md": None, "error": None,
            "reviewed": None, "imported_count": 0,   # reviewed: None|imported|skipped
        })

    if not groups:
        return jsonify(ok=False, error="没有有效的题干文件"), 400

    batch_id = uuid.uuid4().hex
    with _batch_jobs_lock:
        _batch_jobs[batch_id] = {"status": "converting", "groups": groups,
                                 "current_idx": 0, "files_cleaned": False}
    threading.Thread(target=_convert_batch_worker, args=(batch_id,),
                     daemon=True).start()
    # 跳到看板页（前端 window.location）
    return jsonify(ok=True, batch_id=batch_id, count=len(groups),
                   dashboard=url_for("batch_dashboard", batch_id=batch_id))


@app.route("/batch-convert/status/<batch_id>")
def batch_convert_status(batch_id):
    """看板轮询：整批状态 + 各组状态（含 gid/是否已审/入库数），供实时刷新。"""
    with _batch_jobs_lock:
        batch = _batch_jobs.get(batch_id)
        if not batch:
            return jsonify(status="error", error="任务不存在"), 404
        groups = [{"gid": g["gid"], "filename": g["filename"],
                   "status": g["status"], "error": g["error"],
                   "reviewed": g.get("reviewed"),
                   "imported_count": g.get("imported_count", 0)}
                  for g in batch["groups"]]
        all_terminal = all(_group_terminal(g) for g in batch["groups"])
        return jsonify(status=batch["status"], total=len(batch["groups"]),
                       groups=groups, all_done=all_terminal)


@app.route("/batch-convert/<batch_id>/cancel", methods=["POST"])
def batch_convert_cancel(batch_id):
    """取消整批：清该批上传文件、释放内存。已在转换的组无法真正中断，但
    未审核的结果不会入库。"""
    _clean_batch_uploads(batch_id)
    return jsonify(ok=True)


@app.route("/batch/<batch_id>")
def batch_dashboard(batch_id):
    """方式四看板页：列出各组转换/审核状态。转好的组亮出「审核入库」，
    转换在后台继续，用户可任意挑已就绪的组处理。"""
    with _batch_jobs_lock:
        batch = _batch_jobs.get(batch_id)
        if not batch:
            flash("批量任务已过期或不存在", "err")
            return redirect(url_for("import_md"))
        groups = [{"gid": g["gid"], "filename": g["filename"],
                   "status": g["status"], "error": g["error"],
                   "reviewed": g.get("reviewed"),
                   "imported_count": g.get("imported_count", 0)}
                  for g in batch["groups"]]
    return render_template("batch_dashboard.html", batch_id=batch_id, groups=groups)


@app.route("/batch/<batch_id>/group/<int:gid>")
def batch_group_review(batch_id, gid):
    """进某组的校对页（复用 import.html 的 preview）。仅转换完成的组可进。"""
    with _batch_jobs_lock:
        batch = _batch_jobs.get(batch_id)
        if not batch:
            flash("批量任务已过期或不存在", "err")
            return redirect(url_for("import_md"))
        g = next((x for x in batch["groups"] if x["gid"] == gid), None)
        if not g:
            abort(404)
        if g["status"] != "done" or not g["md"]:
            flash("该组尚未转换完成", "err")
            return redirect(url_for("batch_dashboard", batch_id=batch_id))
        md, job_id, filename = g["md"], g["job_id"], g["filename"]

    preview, all_cols = _build_import_preview(md)
    batch_tag = Path(filename).stem
    return render_template(
        "import.html", preview=preview, raw=md, batch_tag=batch_tag,
        all_collections=all_cols, job_id=job_id,
        batch_id=batch_id, batch_gid=gid, queue_filename=filename)


@app.route("/batch/<batch_id>/group/<int:gid>/skip", methods=["POST"])
def batch_group_skip(batch_id, gid):
    """跳过某组（不入库），标记已审，回看板。"""
    with _batch_jobs_lock:
        batch = _batch_jobs.get(batch_id)
        if batch:
            g = next((x for x in batch["groups"] if x["gid"] == gid), None)
            if g and g.get("reviewed") is None:
                g["reviewed"] = "skipped"
    _maybe_finish_batch(batch_id)
    flash("已跳过该组", "ok")
    return redirect(url_for("batch_dashboard", batch_id=batch_id))


# ---------------------------------------------------------------------------
# 查重
# ---------------------------------------------------------------------------


@app.route("/dedup")
def dedup_page():
    """扫全库找重复组（完全重复 + 相似度）。"""
    threshold = request.args.get("threshold", type=float) or 0.85
    threshold = min(max(threshold, 0.5), 1.0)   # 限定合理范围
    rows = filestore.list_questions()
    items = [{"id": r["id"], "body": r["body"], "type": r["type"],
              "source": r["source"]} for r in rows]
    groups = dedup.find_duplicates(items, threshold=threshold)
    return render_template("dedup.html", groups=groups, threshold=threshold,
                           total=len(items))


@app.route("/dedup/delete", methods=["POST"])
def dedup_delete():
    """批量删除查重页勾选的题目（默认全选删除，用户取消勾选保留的）。"""
    ids = {v for v in request.form.getlist("del") if v}
    if not ids:
        flash("没有勾选要删除的题目", "err")
        return redirect(request.referrer or url_for("dedup_page"))
    for qid in ids:
        filestore.delete_question(qid)
    flash(f"已删除 {len(ids)} 道题", "ok")
    return redirect(request.referrer or url_for("dedup_page"))


# ---------------------------------------------------------------------------
# 回收站：题目 + 题集软删除，手动恢复/彻底删除/清空
# ---------------------------------------------------------------------------


@app.route("/recycle-bin")
def recycle_bin():
    questions = filestore.list_deleted_questions()
    folders = filestore.list_deleted_collections()
    return render_template("recycle_bin.html", questions=questions, folders=folders)


@app.route("/recycle-bin/question/<qid>/restore", methods=["POST"])
def recycle_restore_question(qid):
    filestore.restore_question(qid)
    flash("题目已恢复", "ok")
    return redirect(url_for("recycle_bin"))


@app.route("/recycle-bin/question/<qid>/purge", methods=["POST"])
def recycle_purge_question(qid):
    filestore.purge_question(qid)
    flash("题目已彻底删除", "ok")
    return redirect(url_for("recycle_bin"))


@app.route("/recycle-bin/folder/<fid>/restore", methods=["POST"])
def recycle_restore_folder(fid):
    folders = {f["id"]: f for f in filestore.list_deleted_collections()}
    folder = folders.get(fid)
    if not folder:
        abort(404)
    try:
        filestore.restore_collection(fid)
        flash(f"题集「{folder['name']}」已恢复", "ok")
    except FileExistsError:
        flash(f"恢复失败：已存在同名题集「{folder['name']}」，请先改名或处理", "err")
    return redirect(url_for("recycle_bin"))


@app.route("/recycle-bin/folder/<fid>/purge", methods=["POST"])
def recycle_purge_folder(fid):
    filestore.purge_collection(fid)
    flash("题集已彻底删除", "ok")
    return redirect(url_for("recycle_bin"))


@app.route("/recycle-bin/empty", methods=["POST"])
def recycle_empty():
    filestore.empty_recycle_bin()
    flash("回收站已清空", "ok")
    return redirect(url_for("recycle_bin"))


# ---------------------------------------------------------------------------
# 设置：识别模型（LLM）
# ---------------------------------------------------------------------------


@app.route("/settings")
def settings_page():
    """设置页。目前只有「识别模型」一块，日后要加别的设置直接往这页加面板。"""
    data_providers = providers.list_llm_providers()
    active = providers.get_active_llm_provider()
    active_id = active["id"] if active else None
    enriched = [{**p, "is_active": p["id"] == active_id} for p in data_providers]
    return render_template("settings.html", providers=enriched,
                           default_max_tokens=llm_client.MAX_TOKENS_DEFAULT)


@app.route("/settings/llm", methods=["POST"])
def settings_llm():
    """识别模型的增删启停。llm_action ∈ add|activate|deactivate|remove。"""
    action = request.form.get("llm_action", "")
    if action == "add":
        name = request.form.get("llm_name", "").strip()
        base_url = request.form.get("llm_base_url", "").strip()
        api_key = request.form.get("llm_api_key", "").strip()
        model = request.form.get("llm_model", "").strip()
        if not (name and base_url and api_key and model):
            flash("名称、Base URL、API Key、模型名都要填", "error")
            return redirect(url_for("settings_page"))
        max_tokens = llm_client.clamp_max_tokens(
            request.form.get("llm_max_tokens"))
        try:
            enc = crypto_utils.encrypt_token(api_key)
        except crypto_utils.CryptoError as e:
            flash(str(e), "error")
            return redirect(url_for("settings_page"))
        # Base URL 先归一化再存：列表里显示的就是实际会请求的地址，
        # 免得用户对着自己填的裸域名怀疑是不是没生效。
        pid = providers.add_llm_provider(
            name=name, base_url=llm_client.normalize_base_url(base_url),
            api_key_enc=enc, model=model, max_tokens=max_tokens)
        if request.form.get("llm_activate_now") in ("1", "true", "on"):
            providers.set_active_llm_provider(pid)
        flash(f"已添加识别模型「{name}」", "ok")
    elif action in ("activate", "deactivate", "remove"):
        pid = request.form.get("llm_id", "").strip()
        if action != "deactivate" and not pid:
            flash("参数不正确", "error")
            return redirect(url_for("settings_page"))
        if action == "activate":
            providers.set_active_llm_provider(pid)
            flash("已切换识别模型", "ok")
        elif action == "deactivate":
            providers.deactivate_llm_providers()
            flash("已停用，识别将回落到内置默认 DeepSeek", "ok")
        else:
            providers.remove_llm_provider(pid)
            flash("已删除", "ok")
    else:
        flash("未知操作", "error")
    return redirect(url_for("settings_page"))


# ---------------------------------------------------------------------------
# 批量导入
# ---------------------------------------------------------------------------


def _build_import_preview(raw: str):
    """把规范化 md 文本切成预览题卡列表，附查重标记。返回 (preview, all_cols)。

    单文件预览与批量 md 队列共用，保证两条路的切分/查重规则一致。
    """
    blocks = importer.split_questions(raw)
    existing_fps = {dedup.fingerprint(r["body"]) for r in filestore.list_questions()}
    all_cols = filestore.all_collections()
    preview = []
    seen_fps = set()
    for i, b in enumerate(blocks):
        # 先读块首题型标签定类型（逐块识别路径会打 `[单选]` 这类标签），再把标签
        # 剥掉——之后的切解析/查重/入库正文都用干净文本，标签不能进库
        qtype = importer.guess_type(b)
        b = importer.strip_type_tag(b)
        stem, solution = importer.split_solution(b)
        fp = dedup.fingerprint(stem)   # 指纹用题干，不含解析
        if fp in existing_fps:
            dup = "库中已存在"
        elif fp in seen_fps:
            dup = "本批重复"
        else:
            dup = None
        seen_fps.add(fp)
        preview.append({"idx": i, "body": stem, "solution": solution or "",
                        "type": qtype, "dup": dup})
    return preview, all_cols


# 批量 md 队列：queue_id -> {files: [{name, text}], pos: 已处理到第几个（0-based）}
# 本地单用户、内存态即可，会话结束丢弃。逐个文件过校对页导入/跳过后推进 pos。
_md_queues: dict[str, dict] = {}
_md_queues_lock = threading.Lock()


@app.route("/import/batch", methods=["POST"])
def import_batch_start():
    """接收多个 md 文件，建队列，重定向到第一个文件的校对页。"""
    files = request.files.getlist("md_files")
    items = []
    for f in files:
        if not f or not f.filename:
            continue
        if Path(f.filename).suffix.lower() not in (".md", ".markdown", ".txt"):
            continue
        text = f.read().decode("utf-8", errors="replace")
        items.append({"name": f.filename, "text": text})
    if not items:
        flash("未选择有效的 .md 文件", "err")
        return redirect(url_for("import_md"))

    queue_id = uuid.uuid4().hex
    with _md_queues_lock:
        _md_queues[queue_id] = {"files": items, "pos": 0}
    flash(f"已载入 {len(items)} 个文件，开始逐个校对", "ok")
    return redirect(url_for("import_queue_step", queue_id=queue_id))


@app.route("/import/queue/<queue_id>")
def import_queue_step(queue_id):
    """展示队列中当前文件的切分校对页（GET，供跳过后重定向进入）。"""
    with _md_queues_lock:
        q = _md_queues.get(queue_id)
        if not q:
            flash("批量任务已过期或不存在", "err")
            return redirect(url_for("import_md"))
        pos = q["pos"]
        files = q["files"]
        total = len(files)
        batch_id = q.get("batch_id")
        if pos >= total:
            _md_queues.pop(queue_id, None)
            flash(f"批量导入完成，共处理 {total} 个文件", "ok")
            # 方式四整批校对完：清该批上传文件与内存
            if batch_id:
                _clean_batch_uploads(batch_id)
            return redirect(url_for("index"))
        cur = files[pos]

    preview, all_cols = _build_import_preview(cur["text"])
    # 文件名去扩展名作整批标签默认值（方式四 name 形如「第 N 组 · 原名.pdf」，
    # 取原名部分去扩展名更合适）
    raw_name = cur["name"].split(" · ", 1)[-1]
    batch_tag = Path(raw_name).stem
    return render_template(
        "import.html", preview=preview, raw=cur["text"],
        batch_tag=batch_tag, all_collections=all_cols,
        job_id=cur.get("job_id", ""),
        queue_id=queue_id, queue_pos=pos + 1, queue_total=total,
        queue_filename=cur["name"])


@app.route("/import/queue/<queue_id>/skip", methods=["POST"])
def import_queue_skip(queue_id):
    """跳过当前文件，推进到下一个。"""
    with _md_queues_lock:
        q = _md_queues.get(queue_id)
        if q:
            q["pos"] += 1
    return redirect(url_for("import_queue_step", queue_id=queue_id))


@app.route("/import", methods=["GET", "POST"])
def import_md():
    if request.method == "GET":
        return render_template("import.html", preview=None)

    raw = request.form.get("md", "")
    # 上传文件优先
    file = request.files.get("file")
    if file and file.filename:
        raw = file.read().decode("utf-8", errors="replace")

    action = request.form.get("action", "preview")

    if action == "preview":
        # 默认整批标签：上传路径带来的文件名（去扩展名）
        batch_tag = request.form.get("batch_tag", "").strip()
        job_id = request.form.get("job_id", "").strip()  # 上传路径才有，用于左侧 PDF 对照
        preview, all_cols = _build_import_preview(raw)
        return render_template("import.html", preview=preview, raw=raw,
                               batch_tag=batch_tag, all_collections=all_cols,
                               job_id=job_id)

    # confirm：按 body_<idx> 读逐题（用户改后）内容入库，不重新 split raw。
    # keep=保留的 idx（升序）。整批置顶、保持顺序。
    keep = sorted(int(x) for x in request.form.getlist("keep") if x.isdigit())
    batch_tags = [t.strip() for t in request.form.get("tags", "").split(",") if t.strip()]
    target_col = request.form.get("collection", "").strip()   # 目标题集（可空=仅入库）
    # 批量 md 队列：导入完当前文件后推进到下一个；空提交也推进（等于跳过）
    queue_id = request.form.get("queue_id", "").strip()
    # 方式四：来自某组校对，导入后标记该组已审、回看板
    batch_id = request.form.get("batch_id", "").strip()
    batch_gid = request.form.get("batch_gid", "")
    batch_gid = int(batch_gid) if batch_gid.isdigit() else None

    def _mark_group_reviewed(imported_count):
        if not batch_id:
            return
        with _batch_jobs_lock:
            b = _batch_jobs.get(batch_id)
            if b:
                g = next((x for x in b["groups"] if x["gid"] == batch_gid), None)
                if g and g.get("reviewed") is None:
                    g["reviewed"] = "imported" if imported_count else "skipped"
                    g["imported_count"] = imported_count
        _maybe_finish_batch(batch_id)

    def _after_import(imported_count=0):
        """入库后：方式四回看板；md 队列推进下一个；否则回题库。"""
        if batch_id:
            _mark_group_reviewed(imported_count)
            return redirect(url_for("batch_dashboard", batch_id=batch_id))
        if queue_id:
            with _md_queues_lock:
                q = _md_queues.get(queue_id)
                if q:
                    q["pos"] += 1
            return redirect(url_for("import_queue_step", queue_id=queue_id))
        return redirect(url_for("index"))

    # 收集每题最终内容
    chosen = []
    for idx in keep:
        body = request.form.get(f"body_{idx}", "").strip()
        solution = request.form.get(f"solution_{idx}", "").strip() or ""
        diff = request.form.get(f"diff_{idx}", "").strip()
        if diff not in ("1", "2", "3", "4", "5"):
            diff = ""
        starred = request.form.get(f"star_{idx}") in ("1", "true", "on")
        if body:
            chosen.append((idx, body, solution, diff, starred))
    if not chosen:
        if batch_id or queue_id:
            flash("未勾选任何题目，已跳过", "ok")
            return _after_import(0)
        flash("没有勾选任何题目", "err")
        return redirect(url_for("import_md"))
    new_ids = []
    for idx, body, solution, diff, starred in chosen:
        per = [t.strip() for t in request.form.get(f"tag_{idx}", "").split(",") if t.strip()]
        tags = list(dict.fromkeys(batch_tags + per))
        # 题型优先取校对页逐题下拉（用户可手改），非法/缺失才回退正文特征猜测；
        # body 已是校对页剥过标签的干净文本，这里再兜底 strip 一次防标签残留入库
        body = importer.strip_type_tag(body)
        qtype = request.form.get(f"type_{idx}", "").strip()
        if qtype not in config.QUESTION_TYPES:
            qtype = importer.guess_type(body)
        qid = filestore.create_question(body, solution=solution, qtype=qtype,
                                        tags=tags, difficulty=diff)
        if starred:
            filestore.toggle_starred(qid)
        new_ids.append(qid)
    if target_col:
        for qid in new_ids:
            filestore.add_to_collection(qid, target_col)
    msg = f"已导入 {len(chosen)} 道题"
    if target_col:
        msg += "并加入所选题集"
    flash(msg, "ok")
    return _after_import(len(chosen))


# ---------------------------------------------------------------------------
# 导出
# ---------------------------------------------------------------------------


def _collect_questions(scope: str) -> list[dict]:
    """按 scope 从库里取题目 dict 列表（export 与 preview 共用）。"""
    tags = [t for t in request.form.get("tags", "").split(",") if t.strip()]
    match = request.form.get("match", "and")
    type_ = request.form.get("type") or ""
    if scope == "selected":
        rows = filestore.list_questions(selected_only=True)
    elif scope == "filtered":
        rows = filestore.list_questions(tags=tags, match=match, qtype=type_)
    else:
        rows = filestore.list_questions()
    return [{"id": r["id"], "body": r["body"], "type": r["type"],
             "solution": r["solution"], "img_align": r["img_align"],
             "img_width": r["img_width"], "img_split": r["img_split"],
             # 多图逐图排版设置（见 exporter._parse_layouts）；
             # 老题为空列表时导出退回 img_width/img_align 的单图行为
             "img_layouts": r["img_layouts"]}
            for r in rows]


def _read_export_params():
    """从 request.form 读取所有导出参数（export 与 preview 共用）。"""
    return dict(
        scope=request.form.get("scope", "selected"),
        mode=request.form.get("mode", "list"),
        title=request.form.get("title", "").strip() or "试卷",
        keypoints=request.form.get("keypoints", ""),
        solution_mode=request.form.get("solution_mode", "none"),
        fullpage_ids=[x for x in request.form.getlist("fullpage") if x],
        header_footer={
            "header_left": request.form.get("header_left", ""),
            "header_center": request.form.get("header_center", ""),
            "header_right": request.form.get("header_right", ""),
            "footer_left": request.form.get("footer_left", ""),
            "footer_center": request.form.get("footer_center", ""),
            "footer_right": request.form.get("footer_right", ""),
        },
        # 标准试卷（exam_std）专属：科目/保密说明/卷首说明/信息栏/各大题分值说明
        std_opts={
            "subject": request.form.get("subject", ""),
            "info_bar": request.form.get("info_bar", "") in ("1", "true", "on"),
            "secret_notice": request.form.get("secret_notice", ""),
            "exam_notes": request.form.get("exam_notes", ""),
            "section_points": {
                "choice": request.form.get("points_choice", ""),
                "blank": request.form.get("points_blank", ""),
                "solve": request.form.get("points_solve", ""),
            },
        },
    )


@app.route("/preview", methods=["POST"])
def preview():
    """生成真实 PDF 并内联返回，供前端 iframe 嵌入预览（所见即所得）。"""
    p = _read_export_params()
    questions = _collect_questions(p["scope"])
    if not questions:
        return "没有可预览的题目", 400

    try:
        out_path = exporter.export(
            questions, title=p["title"], fmt="pdf", mode=p["mode"],
            keypoints=p["keypoints"], fullpage_ids=p["fullpage_ids"],
            header_footer=p["header_footer"], solution_mode=p["solution_mode"],
            std_opts=p["std_opts"])
    except exporter.ExportError as e:
        return f"预览生成失败：{e}", 500

    # 内联显示（非下载），iframe 可直接嵌入
    return send_file(out_path, mimetype="application/pdf", as_attachment=False)


@app.route("/export", methods=["POST"])
def export():
    p = _read_export_params()
    fmt = request.form.get("fmt", "pdf")             # pdf / tex / zip（仅导出有）

    questions = _collect_questions(p["scope"])
    if not questions:
        flash("没有可导出的题目", "err")
        return redirect(request.referrer or url_for("index"))

    try:
        out_path = exporter.export(
            questions, title=p["title"], fmt=fmt, mode=p["mode"],
            keypoints=p["keypoints"], fullpage_ids=p["fullpage_ids"],
            header_footer=p["header_footer"], solution_mode=p["solution_mode"],
            std_opts=p["std_opts"])
    except exporter.ExportError as e:
        flash(f"导出失败：{e}", "err")
        return redirect(request.referrer or url_for("index"))

    return send_file(out_path, as_attachment=True)


if __name__ == "__main__":
    # 仅本地监听，无鉴权
    app.run(host="127.0.0.1", port=5000, debug=True)
