"""QuizForge 软件版 —— 单机文件式题库 Web 应用。

安全说明：仅监听 127.0.0.1，无鉴权，供本地单人使用。请勿暴露到公网。

运行：
    python app.py
浏览器打开 http://127.0.0.1:5000
"""

from flask import (Flask, render_template, request, redirect, url_for,
                   jsonify, flash, send_file, send_from_directory, abort)
from markupsafe import Markup, escape
from werkzeug.utils import safe_join

import config
import filestore
import importer
import mechfix
import exporter
import service_ports
import license_manager
import device_identity
import desktop_product
import qrender
import dedup
import converter
import blocksplit
import qualcheck
import crypto_utils
import llm_client
import providers
import doc2x_store
import mineru_store
import ui_prefs
import tikz_redraw
import task_store
import cleanup_output
import handouts
import pdf_collection

import os
import hmac
import re
import json
import secrets
import time
import uuid
import logging
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path, PurePosixPath
from PIL import Image

logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = "quizbank-local-dev"  # 本地会话用，非安全敏感
# 整次请求总量上限（见 config.MAX_REQUEST_BYTES）。不设的话 werkzeug 不限，几百组
# 带图的卷子会一路读进内存；设了则超限直接 413，由下面的 errorhandler 转成 JSON。
app.config["MAX_CONTENT_LENGTH"] = config.MAX_REQUEST_BYTES

# 任意网页都能尝试向 127.0.0.1 发写请求，因此本机监听仍需不可猜的写令牌。
# 令牌随进程生成；后端重启后刷新旧页面即可拿到新令牌。
_WRITE_TOKEN = secrets.token_urlsafe(32)
_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


@app.template_global("csrf_token")
def csrf_token() -> str:
    return _WRITE_TOKEN


@app.before_request
def protect_local_writes():
    if request.method not in _UNSAFE_METHODS:
        return None
    supplied = request.headers.get("X-CSRF-Token") or request.form.get("_csrf_token")
    if not isinstance(supplied, str) or not hmac.compare_digest(_WRITE_TOKEN, supplied):
        abort(400, description="页面安全令牌无效，请刷新页面后重试")
    return None

filestore.init_store()


@app.errorhandler(413)
def _too_large(_e):
    """请求超过 MAX_CONTENT_LENGTH 时给一句能看懂的话。

    werkzeug 默认回一页 HTML 413，而方式一的提交是 fetch + `res.json()`——那边
    拿到 HTML 只会抛 SyntaxError，用户看到的是「请求出错：Unexpected token '<'」。
    按发起方分流：批量转换那两条走 JSON，其余走 flash 回原页。
    """
    limit_mb = config.MAX_REQUEST_BYTES // (1024 * 1024)
    msg = (f"本次上传总量超过上限（{limit_mb}MB）。"
           f"请把这一批拆成几批分别提交。")
    if request.path.startswith(("/batch-convert/", "/convert/")):
        return jsonify(ok=False, error=msg), 413
    flash(msg, "err")
    return redirect(request.referrer or url_for("index")), 413


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


@app.template_filter("qbody")
def qbody_filter(q):
    """题目记录 → 结构化正文 HTML（选项分列、图片落位与导出 PDF 同源）。

    与上面的 `qimage` 的分工：`qimage` 只做「转义 + 还原 <img>」，正文靠
    `white-space: pre-wrap` 原样显示，选项会挤成一行；本过滤器走 qrender，把
    选项切成网格、图片按 exporter.plan_figs 落位 —— **规则与 PDF 是同一批函数**，
    所以卡片上看到的版式和导出的试卷一致。`qimage` 保留给不需要结构的地方
    （如导入预览的原始文本）。

    传整条记录而不是 body 字符串：结构判定要用 qtype 和 img_* 那几列，模板里
    逐个传参既啰嗦又容易漏（漏了就静默退化成无结构渲染，很难发现）。
    """
    if not q:
        return Markup("")
    # 本项目 filestore 的题型字段叫 "type"（frontmatter 里也是 type:），
    # qrender/exporter 的参数名沿用服务器版的 qtype —— 在这里对齐，别改任何一边。
    return qrender.render_body(
        q.get("body") or "", q.get("type"),
        img_layouts=q.get("img_layouts"), img_width=q.get("img_width"),
        img_align=q.get("img_align"), img_split=q.get("img_split"))


@app.template_filter("qsolution")
def qsolution_filter(q):
    """题目记录 → 结构化解析 HTML（与 exporter._solution_body 同源）。

    解析不参与四图配选项/选项网格；可独立启用左文右图分栏。
    """
    if not q:
        return Markup("")
    return qrender.render_solution(q.get("solution") or "",
                                   q.get("sol_img_layouts"),
                                   q.get("sol_img_split"))


@app.template_global("qfig_groups")
def qfig_groups(q) -> str:
    """「连续两图」的分组，JSON 串（`[{"ids":[1,2],"row":true}, ...]`）。

    只吐多图组：单图组谈不上并排/堆叠，前端据此决定「并排/堆叠」按钮显不显示，
    以及显示成哪个态。分组照 plan_figs 现算，与卡片正文同源；这样按钮的亮灭
    与眼前那两张图到底并排没并排必然一致。
    JS 里不写 Jinja，故走 data-* 传（同 data-layouts）。
    """
    if not q:
        return ""
    groups = qrender.fig_groups(q.get("body") or "", q.get("type"),
                                q.get("img_layouts"), q.get("img_split"))
    return json.dumps(groups, separators=(",", ":")) if groups else ""


@app.template_global("qsol_fig_groups")
def qsol_fig_groups(q) -> str:
    """解析里「连续两图」的分组，JSON 串，与 qfig_groups 对称（见其 docstring）。

    解析没有题型语境，但有独立的 sol_img_split；换一份正文与布局源后仍复用
    exporter.plan_figs。
    """
    if not q:
        return ""
    groups = qrender.fig_groups(q.get("solution") or "", None,
                                q.get("sol_img_layouts"), q.get("sol_img_split"))
    return json.dumps(groups, separators=(",", ":")) if groups else ""


def _qplan(q) -> dict:
    """这道题的图片编排计划（与卡片正文、导出 PDF 同一个 plan_figs）。"""
    body, _figs = qrender._strip_imgs(q.get("body") or "")
    return exporter.plan_figs(body, q.get("type"), q.get("img_layouts"),
                              q.get("img_split"))


@app.template_global("qsplit_mode")
def qsplit_mode(q) -> str:
    """题卡上**实际生效**的图文分栏模式（""/"opts"/"full"/"sub"）。

    不能直接读 frontmatter 的 `img_split`：带图选择题在用户没设过（值为空）时
    默认就是整题分栏（见 exporter.resolve_split），照原值点亮按钮的结果是——
    正文已经左右分栏了，而三个按钮全是暗的，用户只能靠瞎点找回状态。
    四图配选项生效时不算分栏（那条分支不走 split），返回空。
    """
    if not q:
        return ""
    plan = _qplan(q)
    if plan["pair"]:
        return ""
    has_tail = plan["has_tail"]
    return exporter.resolve_split(q.get("type"), q.get("img_split"),
                                  has_tail) or ""


@app.template_global("qpair_state")
def qpair_state(q) -> str:
    """四图配选项按钮的三态：""=这道题谈不上（不显示按钮）/"on"=生效/"off"=可开未开。

    「谈不上」与「关掉了」必须分开：前者不该出现按钮（非选择题、图数不对），
    后者要显示成暗的好让用户开回来。
    """
    if not q or q.get("type") not in ("单选题", "多选题"):
        return ""
    if _qplan(q)["pair"]:
        return "on"
    return "off" if qrender.pair_applies(q.get("body") or "",
                                         qtype=q.get("type")) else ""


@app.context_processor
def _inject_types():
    """题型列表全模板可用。校对页的逐题题型下拉要用它，而校对页有三个入口
    （单文件/md 队列/方式四看板）都渲染 import.html，逐个传参容易漏。"""
    return {"types": config.QUESTION_TYPES,
            "bank_subject": config.BANK_SUBJECT,
            "bank_subject_label": config.BANK_SUBJECT_LABEL,
            "batch_concurrency": max(
                1, int(getattr(config, "BATCH_CONVERT_CONCURRENCY", 1))),
            # 上传边界一并注入：前端要在提交前就报错（早报错比传半天再失败好），
            # 但那些数只该有一份定义。模板把它们塞进 data-* 交给 JS 读，避免
            # import-upload.js 里再抄一遍常量、改一边忘一边。
            "upload_limits": _upload_limits()}


@app.template_filter("qtype_label")
def _question_type_label(qtype):
    """题型提交值不变，只在模板显示层按题库科目换名。"""
    return config.question_type_label(qtype)


def _upload_limits() -> dict:
    """给前端的上传边界快照（见 config.py 的「上传边界」一节）。"""
    return {
        "max_groups": config.MAX_BATCH_GROUPS,
        "max_batch_files": config.MAX_BATCH_FILES,
        "max_files_per_side": config.MAX_FILES_PER_GROUP_SIDE,
        "max_document_bytes": config.MAX_EXAM_DOCUMENT_BYTES,
        "max_image_bytes": config.MAX_EXAM_IMAGE_BYTES,
        "max_request_bytes": config.MAX_REQUEST_BYTES,
        "max_md_files": config.MAX_MD_FILES,
        "max_md_file_bytes": config.MAX_MD_FILE_BYTES,
        "max_md_batch_bytes": config.MAX_MD_BATCH_BYTES,
        "exam_exts": sorted(config.EXAM_EXTS),
        "image_exts": sorted(config.EXAM_IMAGE_EXTS),
        "md_exts": sorted(config.MD_EXTS),
    }


@app.context_processor
def _inject_ui_prefs():
    """外观偏好（深浅色/主题色/壁纸）注入所有模板。它要出现在 base.html 的
    <html> 和 <body> 标签上，各视图逐个传参必然漏——漏掉的那一页会静默变回
    浅色，很难发现。"""
    prefs = ui_prefs.load()
    return {"ui": {**prefs,
                   "wallpaper_is_video": ui_prefs.is_video_wallpaper(
                       prefs["wallpaper"])}}


@app.context_processor
def _inject_desktop_host():
    """桌面外壳必须在首屏 HTML 就确定，避免整页导航时先闪出浏览器版导航。"""
    return {
        "desktop_host": os.environ.get("QUIZFORGE_DESKTOP") == "1",
        "desktop_version": desktop_product.PRODUCT_VERSION,
    }


@app.context_processor
def _inject_nav_badge():
    """导航栏「转换任务」后面的未处理批次数。放 context_processor 是因为它要
    出现在 base.html，每个页面都要算——各视图逐个传参必然漏。
    """
    with _batch_jobs_lock:
        n = sum(1 for b in _batch_jobs.values()
                if not all(_group_terminal(g) for g in b["groups"]))
    return {"nav_batch_count": n}


@app.template_global()
def static_v(filename: str) -> str:
    """静态资源 URL，按文件 mtime 追加 `?v=` 做缓存击穿。

    没有它的话，改完 static/js/*.js 或 style.css 后浏览器会拿旧的缓存副本，
    表现为"代码明明改了、页面没反应"。Obsidian 插件用的是内嵌 webview，
    自带缓存且没有 Ctrl+Shift+R 可按，比独立浏览器更需要这个。

    取不到 mtime（文件不存在）时退化成不带参数的普通 URL，不抛错 ——
    少一个缓存参数远好过整页 500。
    """
    url = url_for("static", filename=filename)
    try:
        mtime = int((Path(app.static_folder) / filename).stat().st_mtime)
    except OSError:
        return url
    return f"{url}?v={mtime}"


# ---------------------------------------------------------------------------
# 题目列表 + 筛选
# ---------------------------------------------------------------------------


@app.route("/healthz")
def healthz():
    """健康检查：供 Obsidian 插件判断后端是否已就绪 / 是否是自己认识的服务。

    bank 返回题库绝对路径，插件用它换算题目 md 在 vault 里的相对路径。
    """
    return jsonify({
        "app": "quizforge",
        "status": "ok",
        "bank": str(config.BANK_DIR),
        # 插件卸载时仍只结束自己持有的 ChildProcess；pid/project 只供用户明确点击
        # 「重启后端」时确认并回收插件重载前留下的同项目实例，不能只凭端口杀进程。
        "pid": os.getpid(),
        "project": str(config.BASE_DIR),
    })


@app.route("/about")
def about_page():
    """产品信息与本机环境诊断；只读且不会探测任何网络服务。"""
    return render_template(
        "about.html", report=desktop_product.environment_report(),
        welcome=False, demo_created=False,
    )


@app.route("/welcome")
def welcome_page():
    """桌面初次启动落点；浏览器直接访问也保持只读。"""
    return render_template(
        "about.html", report=desktop_product.environment_report(),
        welcome=True, demo_created=request.args.get("demo") == "1",
    )


@app.route("/workspace")
def workspace_page():
    """独立桌面版的常驻外壳；业务页面放进同源 iframe，资料库单独保活。"""
    initial_path = (request.args.get("path") or "/").strip()
    # 这里只接受本站绝对路径。禁止 //host 与递归嵌套 /workspace，避免本地外壳被
    # 当成任意站点 iframe 容器；业务接口仍由各自路由继续做完整参数校验。
    if (not initial_path.startswith("/") or initial_path.startswith("//")
            or initial_path.split("?", 1)[0] == "/workspace"):
        initial_path = "/"
    return render_template("workspace.html", initial_path=initial_path)


# ---------------------------------------------------------------------------
# 资料库：当前题库根目录内的通用 Markdown / PDF / 图片阅读器
# ---------------------------------------------------------------------------

_LIBRARY_MARKDOWN_EXTS = frozenset({".md", ".markdown"})
_LIBRARY_PDF_EXTS = frozenset({".pdf"})
_LIBRARY_IMAGE_EXTS = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg",
})
_LIBRARY_FILE_EXTS = (_LIBRARY_MARKDOWN_EXTS | _LIBRARY_PDF_EXTS
                       | _LIBRARY_IMAGE_EXTS)
_LIBRARY_PAGE_SIZE = 300
_LIBRARY_TEXT_LIMIT = 8 * 1024 * 1024


def _library_path(raw: str, *, root_allowed: bool = False) -> tuple[Path, str]:
    """把客户端相对路径收敛到 BANK_DIR；隐藏路径与越界符号链接一律拒绝。"""
    value = str(raw or "").strip().replace("\\", "/")
    rel = PurePosixPath(value)
    parts = tuple(part for part in rel.parts if part not in ("", "."))
    if rel.is_absolute() or any(part == ".." or part.startswith(".") for part in parts):
        abort(404)
    root = config.BANK_DIR.resolve()
    target = root.joinpath(*parts).resolve()
    if target != root and root not in target.parents:
        abort(404)
    if target == root and not root_allowed:
        abort(404)
    return target, PurePosixPath(*parts).as_posix() if parts else ""


def _library_kind(path: Path) -> str | None:
    suffix = path.suffix.lower()
    if suffix in _LIBRARY_MARKDOWN_EXTS:
        return "markdown"
    if suffix in _LIBRARY_PDF_EXTS:
        return "pdf"
    if suffix in _LIBRARY_IMAGE_EXTS:
        return "image"
    return None


def _library_entry_kind(path: Path, rel: str) -> str | None:
    """资料库里把保留目录下的 Markdown 标成讲义，点击时进入专用编辑器。"""
    kind = _library_kind(path)
    parts = PurePosixPath(rel).parts
    if kind == "markdown" and parts and parts[0] == config.HANDOUTS_DIR.name:
        return "handout"
    return kind


def _library_natural_key(name: str) -> tuple:
    return tuple(int(part) if part.isdigit() else part.casefold()
                 for part in re.split(r"(\d+)", name))


@app.route("/library")
def library_page():
    return render_template("library.html")


@app.route("/api/library/children")
def library_children():
    directory, rel = _library_path(request.args.get("path", ""), root_allowed=True)
    if not directory.is_dir():
        return jsonify(ok=False, error="文件夹不存在"), 404
    try:
        offset = max(0, int(request.args.get("offset") or 0))
    except ValueError:
        return jsonify(ok=False, error="列表位置无效"), 400
    entries = []
    try:
        children = list(directory.iterdir())
    except OSError as exc:
        return jsonify(ok=False, error=f"无法读取文件夹：{exc}"), 400
    for child in children:
        if child.name.startswith(".") or child.is_symlink():
            continue
        child_rel = (PurePosixPath(rel) / child.name).as_posix() if rel else child.name
        if child.is_dir():
            entries.append({"name": child.name, "path": child_rel, "kind": "folder"})
            continue
        kind = _library_entry_kind(child, child_rel)
        if kind:
            entries.append({
                "name": child.name, "path": child_rel, "kind": kind,
                "size": child.stat().st_size,
            })
    entries.sort(key=lambda item: (
        item["kind"] != "folder", _library_natural_key(item["name"])))
    page = entries[offset:offset + _LIBRARY_PAGE_SIZE]
    next_offset = offset + len(page)
    return jsonify(ok=True, path=rel, entries=page, total=len(entries),
                   next_offset=next_offset, done=next_offset >= len(entries))


@app.route("/api/library/read")
def library_read():
    target, rel = _library_path(request.args.get("path", ""))
    if not target.is_file() or target.suffix.lower() not in _LIBRARY_MARKDOWN_EXTS:
        return jsonify(ok=False, error="Markdown 文件不存在"), 404
    size = target.stat().st_size
    if size > _LIBRARY_TEXT_LIMIT:
        return jsonify(ok=False, error="Markdown 文件超过 8 MB，无法在软件内打开"), 413
    try:
        with target.open("r", encoding="utf-8-sig", newline="") as handle:
            text = filestore.normalize_newlines(handle.read())
    except (OSError, UnicodeDecodeError) as exc:
        return jsonify(ok=False, error=f"无法按 UTF-8 读取文件：{exc}"), 400
    # 纳秒时间戳通常是 19 位，超过 JavaScript 的安全整数上限。必须作为十进制
    # 字符串交给前端原样回传，否则 JSON.parse 取整后每次保存都会被误判为外部修改。
    return jsonify(ok=True, path=rel, name=target.name, text=text,
                   mtime=str(target.stat().st_mtime_ns))


@app.route("/api/library/write", methods=["POST"])
def library_write():
    payload = request.get_json(silent=True) or {}
    target, rel = _library_path(payload.get("path", ""))
    if not target.is_file() or target.suffix.lower() not in _LIBRARY_MARKDOWN_EXTS:
        return jsonify(ok=False, error="Markdown 文件不存在"), 404
    text = payload.get("text")
    expected_mtime = payload.get("mtime")
    if not isinstance(text, str):
        return jsonify(ok=False, error="Markdown 内容无效"), 400
    if (isinstance(expected_mtime, str)
            and 1 <= len(expected_mtime) <= 32
            and expected_mtime.isdecimal()):
        expected_mtime = int(expected_mtime)
    elif isinstance(expected_mtime, int) and not isinstance(expected_mtime, bool):
        # 兼容 0.7.0 及更早的本机页面请求；新版页面不会再把版本标记转成 Number。
        pass
    else:
        return jsonify(ok=False, error="文件版本无效，请重新打开后再保存"), 400
    if len(text.encode("utf-8")) > _LIBRARY_TEXT_LIMIT:
        return jsonify(ok=False, error="Markdown 文件超过 8 MB，无法在软件内保存"), 413
    try:
        saved, mtime = filestore.write_markdown_text(
            target, text, expected_mtime)
    except OSError as exc:
        return jsonify(ok=False, error=f"无法保存文件：{exc}"), 400
    if not saved:
        return jsonify(
            ok=False,
            error="文件已被 Obsidian 或其他程序修改。请重新打开后合并内容，当前草稿不会丢失。",
            mtime=str(mtime),
        ), 409
    return jsonify(ok=True, path=rel, mtime=str(mtime))


# ---------------------------------------------------------------------------
# 讲义工作台
# ---------------------------------------------------------------------------


@app.route("/handouts")
def handouts_page():
    initial_path = (request.args.get("path") or "").strip().replace("\\", "/")
    return render_template("handouts.html", initial_path=initial_path)


@app.route("/api/handouts")
def handouts_list():
    return jsonify(ok=True, documents=handouts.list_documents())


@app.route("/api/handouts", methods=["POST"])
def handouts_create():
    payload = request.get_json(silent=True) or {}
    try:
        result = handouts.create_document(
            payload.get("title") or "新建讲义",
            page_format=payload.get("page_format") or "a4",
            columns=payload.get("columns") or 1,
        )
    except (handouts.HandoutError, FileExistsError, OSError) as exc:
        return jsonify(ok=False, error=str(exc)), 400
    return jsonify(ok=True, **result), 201


@app.route("/api/handouts/read")
def handouts_read():
    try:
        result = handouts.read_document(request.args.get("path", ""))
    except FileNotFoundError:
        return jsonify(ok=False, error="讲义不存在"), 404
    except (handouts.HandoutError, OSError, UnicodeError) as exc:
        return jsonify(ok=False, error=str(exc)), 400
    return jsonify(ok=True, **result)


@app.route("/api/handouts/write", methods=["POST"])
def handouts_write():
    payload = request.get_json(silent=True) or {}
    try:
        result = handouts.write_document(
            payload.get("path", ""), payload.get("metadata") or {},
            payload.get("body") or "", payload.get("mtime"),
        )
    except FileNotFoundError:
        return jsonify(ok=False, error="讲义不存在"), 404
    except (handouts.HandoutError, OSError, UnicodeError) as exc:
        return jsonify(ok=False, error=str(exc)), 400
    if result.get("conflict"):
        return jsonify(error="文件已被外部修改，自动保存已暂停", **result), 409
    return jsonify(**result)


@app.route("/api/handouts/delete", methods=["POST", "DELETE"])
def handouts_delete():
    payload = request.get_json(silent=True) or {}
    try:
        result = handouts.delete_document(
            payload.get("path", ""), payload.get("mtime"))
    except FileNotFoundError:
        return jsonify(ok=False, error="讲义不存在"), 404
    except (handouts.HandoutError, OSError, UnicodeError) as exc:
        return jsonify(ok=False, error=str(exc)), 400
    if result.get("conflict"):
        return jsonify(error="文件已被外部修改，未执行删除", **result), 409
    return jsonify(**result)


@app.route("/api/handouts/save-as", methods=["POST"])
def handouts_save_as():
    payload = request.get_json(silent=True) or {}
    try:
        result = handouts.create_document(
            payload.get("title") or "讲义副本",
            body=payload.get("body") or "",
            metadata=payload.get("metadata") or {},
        )
    except (handouts.HandoutError, FileExistsError, OSError) as exc:
        return jsonify(ok=False, error=str(exc)), 400
    return jsonify(ok=True, **result), 201


@app.route("/api/handouts/selected")
def handouts_selected():
    return jsonify(ok=True, questions=handouts.selected_question_summaries())


@app.route("/api/handouts/question/<qid>")
def handouts_question_snapshot(qid):
    try:
        snapshot = handouts.question_snapshot(qid)
    except KeyError:
        return jsonify(ok=False, error="原题不存在或已删除"), 404
    block_id = handouts.new_block_id()
    metadata = {
        **snapshot,
        "number_override": None,
        "solution_placement": "inherit",
        "render_confirmed": False,
    }
    return jsonify(ok=True, block_id=block_id, metadata=metadata,
                   body=snapshot["body"], solution=snapshot["solution"])


def _handout_export_payload():
    payload = request.get_json(silent=True) or {}
    metadata = payload.get("metadata")
    body = payload.get("body")
    if not isinstance(metadata, dict) or not isinstance(body, str):
        raise handouts.HandoutError("讲义导出数据无效")
    if len(body.encode("utf-8")) > handouts.MAX_DOCUMENT_BYTES:
        raise handouts.HandoutError("讲义超过 8 MiB 上限")
    return metadata, body


@app.route("/api/handouts/render-question", methods=["POST"])
def handouts_render_question():
    """按正式 XeLaTeX 模板编译单个题卡，返回只含矢量路径的 SVG。"""
    payload = request.get_json(silent=True) or {}
    metadata = payload.get("metadata")
    question = payload.get("question")
    if not isinstance(metadata, dict) or not isinstance(question, dict):
        return jsonify(ok=False, error="题卡编译数据无效"), 400
    try:
        out_path = service_ports.render_handout_question(
            metadata, question, payload.get("position") or 1)
    except (exporter.ExportError, OSError) as exc:
        return jsonify(ok=False, error=f"题卡编译失败：{exc}"), 400
    digest = Path(out_path).stem
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        return jsonify(ok=False, error="题卡缓存文件无效"), 500
    return jsonify(ok=True, url=url_for("handout_rendered_card", digest=digest),
                   cache_key=digest)


@app.route("/api/handouts/preview", methods=["POST"])
def handouts_preview():
    """按当前内存草稿生成真实 PDF；不要求先覆盖磁盘文件。"""
    try:
        metadata, body = _handout_export_payload()
        out_path = service_ports.export_handout_document(
            metadata, body, fmt="pdf")
    except (handouts.HandoutError, exporter.ExportError) as exc:
        return jsonify(ok=False, error=f"讲义预览生成失败：{exc}"), 400
    token = _register_out_file(out_path)
    return jsonify(ok=True, url=url_for("out_file", token=token))


@app.route("/api/handouts/export", methods=["POST"])
def handouts_export():
    """导出 PDF、TeX 或包含图片的 tex.zip。"""
    try:
        metadata, body = _handout_export_payload()
        fmt = str((request.get_json(silent=True) or {}).get("fmt") or "pdf")
        out_path = service_ports.export_handout_document(metadata, body, fmt=fmt)
    except (handouts.HandoutError, exporter.ExportError) as exc:
        return jsonify(ok=False, error=f"讲义导出失败：{exc}"), 400
    title = handouts.normalize_metadata(metadata).get("title") or "讲义"
    filename = (filestore.safe_folder_name(title) or "讲义") + Path(out_path).suffix
    token = _register_out_file(out_path, filename)
    return jsonify(
        ok=True, url=url_for("out_file", token=token, dl=1), filename=filename)


@app.route("/library/raw")
def library_raw():
    target, _rel = _library_path(request.args.get("path", ""))
    kind = _library_kind(target)
    if not target.is_file() or kind not in {"pdf", "image"}:
        abort(404)
    response = send_file(target, as_attachment=False, conditional=True,
                         download_name=target.name)
    response.headers["X-Content-Type-Options"] = "nosniff"
    if target.suffix.lower() == ".svg":
        # SVG 可携带脚本；作为图片显示仍额外加 sandbox，禁止它获得同源脚本能力。
        response.headers["Content-Security-Policy"] = "sandbox"
    return response


@app.route("/api/write-token")
def write_token():
    """供本机批处理脚本轻量读取写令牌，不触发整座题库页面渲染。"""
    return jsonify({"token": _WRITE_TOKEN})


@app.route("/api/tags")
def tags_api():
    """大题库首页按需加载标签，避免首次打开就解析全部 Markdown。"""
    return jsonify(ok=True, tags=filestore.all_tags())


_QUESTION_PAGE_SIZE = 30
_QUESTION_PAGE_TTL = 30 * 60
_QUESTION_PAGE_MAX_SNAPSHOTS = 8
_question_pages: dict[str, dict] = {}
_question_pages_lock = threading.Lock()


def _new_question_page_snapshot(source: list, mode: str, sort: str,
                                card_sort: str | None = None) -> str:
    """保存一次浏览顺序，后续触底只切片，不重复扫描整库。"""
    now = time.monotonic()
    with _question_pages_lock:
        expired = [token for token, page in _question_pages.items()
                   if now - page["at"] > _QUESTION_PAGE_TTL]
        for token in expired:
            _question_pages.pop(token, None)
        while len(_question_pages) >= _QUESTION_PAGE_MAX_SNAPSHOTS:
            oldest = min(_question_pages, key=lambda token: _question_pages[token]["at"])
            _question_pages.pop(oldest, None)
        token = uuid.uuid4().hex
        _question_pages[token] = {
            "source": source, "mode": mode, "sort": sort,
            "card_sort": card_sort or sort, "at": now,
        }
    return token


def _question_page_rows(page: dict, start: int, end: int) -> list[dict]:
    chunk = page["source"][start:end]
    if page["mode"] == "paths":
        return filestore.records_from_paths(chunk)
    rows = list(chunk)
    filestore.refresh_selected(rows)
    return rows


@app.route("/questions/page")
def question_page():
    """无限滚动下一批题卡；快照不可猜且只活 30 分钟。"""
    token = (request.args.get("token") or "").strip()
    try:
        offset = max(0, int(request.args.get("offset") or 0))
    except ValueError:
        return jsonify(ok=False, error="分页位置无效"), 400
    with _question_pages_lock:
        page = _question_pages.get(token)
        if page and time.monotonic() - page["at"] <= _QUESTION_PAGE_TTL:
            page["at"] = time.monotonic()
        else:
            page = None
    if not page:
        return jsonify(ok=False, error="浏览快照已过期，请刷新页面"), 410
    total = len(page["source"])
    end = min(total, offset + _QUESTION_PAGE_SIZE)
    rows = _question_page_rows(page, offset, end)
    html = render_template(
        "_question_page.html", questions=rows, sort=page["sort"],
        question_card_sort=page["card_sort"], types=config.QUESTION_TYPES)
    return jsonify(ok=True, html=html, next_offset=end,
                   total=total, done=end >= total)


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

    # 默认首页不渲染整库题卡。题库大后，图片排版与公式渲染会让一次无目的打开
    # 付出数秒成本；左侧「全部题目」显式带 all=1，筛选与文件夹入口照常查询。
    show_all = request.args.get("all") in ("1", "true", "on")
    has_filter = bool(collection_id or tags or type_ or difficulty
                      or starred_only or search)
    blank = not (show_all or has_filter)

    # 父文件夹默认汇总所有后代题目和原卷。这里的“汇总”只递归建立轻量路径快照，
    # 首屏仍只读取 30 道题，后续随滚动按批加载，不能退回一次创建整年题卡的旧实现。
    collection_children = (filestore.list_collection_children(collection_id)
                           if collection_id else [])
    recursive = bool(collection_children) or (
        request.args.get("recursive") in ("1", "true", "on"))
    explicit_filter = bool(tags or type_ or difficulty or starred_only or search)
    # 保留模板参数兼容文件夹局部切换协议；父文件夹导航概览已由递归汇总取代。
    collection_overview = False

    # 首页需要题目、标签、文件夹计数和下拉列表，但它们都来自同一批 Markdown。
    # 一次请求只扫一遍题库；否则历年卷入库后，四次 rglob/stat 会把页面拖到十几秒。
    tags_deferred = blank
    question_total = 0
    question_page_token = ""
    question_card_sort = sort
    if blank:
        records = []
        questions = []
        all_tags = []
    else:
        # “全部题目”与年份汇总的默认顺序只列路径，首屏不读完整 1.3 万个文件；
        # 搜索/标签/其它排序必须看元数据，仍先建立一次结果快照，但也只渲染首批。
        path_stream = bool(
            sort == "custom" and not explicit_filter and (show_all or recursive))
        if path_stream:
            source = filestore.list_question_paths(collection_id)
            question_total = len(source)
            questions = filestore.records_from_paths(source[:_QUESTION_PAGE_SIZE])
            records = questions
            all_tags = []
            tags_deferred = True
            # 全库自然路径序不是可安全拖拽的单一序列：跨文件夹发送部分 id 会破坏
            # 各卷 frontmatter order。因此浏览流隐藏拖拽手柄，单卷页面仍可自定义。
            question_card_sort = "browse"
            if question_total > len(questions):
                question_page_token = _new_question_page_snapshot(
                    source, "paths", sort, question_card_sort)
        else:
            records = (filestore.collection_records_snapshot(collection_id)
                       if collection_id else filestore.all_records_snapshot())
            source = filestore.list_questions(
                tags=tags, match=match, qtype=type_ or "", sort=sort,
                collection=collection_id, search=search,
                difficulty=difficulty or "", starred=starred_only,
                records=records)
            question_total = len(source)
            questions = source[:_QUESTION_PAGE_SIZE]
            all_tags = filestore.all_tags(records)
            if question_total > len(questions):
                question_page_token = _new_question_page_snapshot(
                    source, "records", sort)
    # 首页侧栏只列顶层并默认折叠；深链接只预载当前路径。完整 642 目录树及移动
    # 目标改为用户点击控件时再加载，不能让一个低频下拉框拖慢每次试卷切换。
    folder_tree = filestore.list_navigation_tree(collection_id)
    all_cols = []
    cur_col = ({"name": collection_id.rsplit("/", 1)[-1]}
               if collection_id else None)
    selected_count = filestore.count_selected()
    # 原卷面板只在选中了某个文件夹时出现：题库根下的「原卷」没有归属，列出来
    # 只会把 vault 根目录里一切非 md 文件都摆上去。
    folder_papers = (filestore.list_papers(collection_id)
                     if collection_id else [])

    return render_template(
        "index.html",
        folder_papers=folder_papers,
        questions=questions,
        question_total=question_total,
        question_page_token=question_page_token,
        question_card_sort=question_card_sort,
        all_tags=all_tags,
        tags_deferred=tags_deferred,
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
        collection_children=collection_children,
        collection_overview=collection_overview,
        blank=blank,
        show_all=show_all,
        active_collection_name=(cur_col["name"] if cur_col else None),
        search=search,
    )


# ---------------------------------------------------------------------------
# 单题 新增 / 编辑 / 删除
# ---------------------------------------------------------------------------


@app.route("/question/new", methods=["GET", "POST"])
def question_new():
    if request.method == "POST":
        qid = _save_from_form()
        # 来自文件夹右键「新增题目」时表单里带着落点，建好直接归入该文件夹
        target_col = (request.form.get("collection") or "").strip()
        if qid and target_col:
            col = filestore.get_collection(target_col)
            if col:
                filestore.add_to_collection(qid, target_col)
        flash("题目已新增", "ok")
        if target_col:
            return redirect(url_for("index", collection=target_col))
        return redirect(url_for("index"))
    # GET：?collection=<id> 预设落点文件夹（右键「新增题目」带过来）
    return render_template("edit.html", q=None, q_tags=[],
                           preset_collection=request.args.get("collection", ""),
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


_MAX_INLINE_MARKDOWN_BYTES = 2 * 1024 * 1024


def _inline_question_payload():
    """校验题卡原地编辑 JSON；字段口径与传统编辑页保持一致。"""
    data = request.get_json(silent=True) or {}
    body = str(data.get("body") or "").strip()
    solution = str(data.get("solution") or "").strip()
    if not body:
        return None, (jsonify(ok=False, error="题目正文不能为空"), 400)
    if len(body.encode("utf-8")) + len(solution.encode("utf-8")) > _MAX_INLINE_MARKDOWN_BYTES:
        return None, (jsonify(ok=False, error="单题正文与解析不能超过 2 MB"), 413)
    type_ = str(data.get("type") or "").strip()
    difficulty = str(data.get("difficulty") or "").strip()
    if type_ and type_ not in config.QUESTION_TYPES:
        return None, (jsonify(ok=False, error="未知题型"), 400)
    if difficulty and difficulty not in {"1", "2", "3", "4", "5"}:
        return None, (jsonify(ok=False, error="难度须为 1-5"), 400)
    tags_value = data.get("tags") or ""
    if isinstance(tags_value, list):
        tags = [str(tag).strip() for tag in tags_value if str(tag).strip()]
    else:
        tags = [tag.strip() for tag in str(tags_value).split(",") if tag.strip()]
    return {
        "body": body,
        "solution": solution,
        "type": type_,
        "difficulty": difficulty,
        "source": str(data.get("source") or "").strip(),
        "tags": tags,
        "card_sort": "custom" if data.get("card_sort") == "custom" else "browse",
    }, None


@app.route("/question/<qid>/preview", methods=["POST"])
def question_inline_preview(qid):
    """实时编译未保存的 Markdown；只读，不改题库文件。"""
    rec = filestore.get_question(qid)
    if not rec:
        return jsonify(ok=False, error="题目不存在"), 404
    payload, error = _inline_question_payload()
    if error:
        return error
    body_html = qrender.render_body(
        payload["body"], payload["type"],
        img_layouts=rec.get("img_layouts"), img_width=rec.get("img_width"),
        img_align=rec.get("img_align"), img_split=rec.get("img_split"))
    solution_html = qrender.render_solution(
        payload["solution"], rec.get("sol_img_layouts"), rec.get("sol_img_split"))
    return jsonify(ok=True, body_html=str(body_html),
                   solution_html=str(solution_html))


@app.route("/question/inline-draft")
def question_inline_draft():
    """返回当前文件夹末尾的新题草稿卡；尚未写入任何 Markdown。"""
    folder = str(request.args.get("collection") or "").strip("/")
    collection = filestore.get_collection(folder) if folder else None
    if folder and not collection:
        return jsonify(ok=False, error="目标文件夹不存在"), 404
    q = {
        "id": "", "body": "", "solution": "", "type": "", "source": "",
        "difficulty": "", "tags": [],
    }
    card_html = render_template(
        "_new_question_card.html", q=q, types=config.QUESTION_TYPES,
        collection=folder,
        collection_name=(collection["name"] if collection else "题库根目录"))
    return jsonify(ok=True, card_html=card_html)


@app.route("/question/inline-preview", methods=["POST"])
def question_inline_create_preview():
    """新题草稿实时编译；与已有题编辑相同，只读且不落盘。"""
    payload, error = _inline_question_payload()
    if error:
        return error
    body_html = qrender.render_body(payload["body"], payload["type"])
    solution_html = qrender.render_solution(payload["solution"])
    return jsonify(ok=True, body_html=str(body_html),
                   solution_html=str(solution_html))


@app.route("/question/inline-create", methods=["POST"])
def question_inline_create():
    """在指定真实文件夹末尾建题，并回渲成普通题卡。"""
    payload, error = _inline_question_payload()
    if error:
        return error
    data = request.get_json(silent=True) or {}
    folder = str(data.get("collection") or "").strip("/")
    if folder and not filestore.get_collection(folder):
        return jsonify(ok=False, error="目标文件夹不存在"), 404
    qid = filestore.create_question(
        payload["body"], solution=payload["solution"],
        qtype=payload["type"], source=payload["source"],
        difficulty=payload["difficulty"], tags=payload["tags"], folder=folder)
    rec = filestore.get_question(qid)
    card_html = render_template(
        "_question_card.html", q=rec, types=config.QUESTION_TYPES,
        question_card_sort=payload["card_sort"])
    return jsonify(ok=True, id=qid, card_html=card_html, message="题目已添加")


@app.route("/question/<qid>/inline", methods=["POST"])
def question_inline_update(qid):
    """题卡内保存并只回渲当前题卡，不做整页跳转。"""
    if not filestore.get_question(qid):
        return jsonify(ok=False, error="题目不存在"), 404
    payload, error = _inline_question_payload()
    if error:
        return error
    filestore.update_question(
        qid, payload["body"], solution=payload["solution"],
        qtype=payload["type"], source=payload["source"],
        difficulty=payload["difficulty"], tags=payload["tags"])
    rec = filestore.get_question(qid)
    card_html = render_template(
        "_question_card.html", q=rec, types=config.QUESTION_TYPES,
        question_card_sort=payload["card_sort"])
    return jsonify(ok=True, card_html=card_html, message="题目已保存")


@app.route("/question/<qid>/delete", methods=["POST"])
def question_delete(qid):
    deleted = filestore.delete_question(qid)
    if request.accept_mimetypes.best == "application/json":
        if not deleted:
            return jsonify(ok=False, error="题目不存在"), 404
        return jsonify(ok=True, deleted=[qid], count=filestore.count_selected(),
                       message="题目已移入回收站")
    flash("题目已删除", "ok")
    return redirect(request.referrer or url_for("index"))


def _save_from_form(qid=None):
    """从表单读字段，新增或更新。返回题目 id（新增时是新生成的那个）。"""
    body = request.form.get("body", "").strip()
    solution = request.form.get("solution", "").strip()
    type_ = request.form.get("type") or ""
    source = request.form.get("source", "").strip()
    difficulty = request.form.get("difficulty") or ""
    tag_names = [t.strip() for t in request.form.get("tags", "").split(",") if t.strip()]
    if qid is None:
        return filestore.create_question(body, solution=solution, qtype=type_,
                                         source=source, difficulty=difficulty,
                                         tags=tag_names)
    filestore.update_question(qid, body, solution=solution, qtype=type_,
                              source=source, difficulty=difficulty,
                              tags=tag_names)
    return qid


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
    """设置题型（AJAX），只回渲当前题卡，不让浏览器刷新整页。"""
    data = request.get_json(silent=True) or {}
    type_ = str(data.get("type", "")).strip()
    if type_ and type_ not in config.QUESTION_TYPES:
        return jsonify(ok=False, error="未知题型"), 400
    filestore.set_type(qid, type_)
    rec = filestore.get_question(qid)
    if not rec:
        return jsonify(ok=False, error="题目不存在"), 404
    card_sort = ("custom" if data.get("card_sort") == "custom" else "browse")
    card_html = render_template(
        "_question_card.html", q=rec, types=config.QUESTION_TYPES,
        question_card_sort=card_sort)
    return jsonify(ok=True, type=type_, card_html=card_html)


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


def _img_field(data):
    """从请求体取"改哪一侧的图"：body（题干）/ solution（解析）。缺省 body。

    非法值返回 None 让调用方回 400，而不是兜底成 body——兜底会把解析图片的设置
    悄悄写进题干那张表，用户看到的是"调了解析的图，题干的图跟着变了"。
    """
    field = str(data.get("field") or "body").strip()
    return field if field in ("body", "solution") else None


@app.route("/question/<qid>/img_align", methods=["POST"])
def question_img_align(qid):
    """设置某张图的水平位置（AJAX）：left/center/right 或空清除。

    index = 正文里图片出现的序号（0 起，缺省 0）；落进 img_layouts，index==0 时
    filestore.set_img_layout 一并回写旧的 img_align 字段。
    field="solution" 时作用于解析里的图（sol_img_layouts），与题干各自独立编号。
    """
    data = request.get_json(silent=True) or {}
    align = str(data.get("align", "")).strip()
    if align and align not in ("left", "center", "right"):
        return jsonify(ok=False, error="非法位置"), 400
    index = _img_index(data)
    if index is None:
        return jsonify(ok=False, error="非法图片序号"), 400
    field = _img_field(data)
    if field is None:
        return jsonify(ok=False, error="非法 field"), 400
    filestore.set_img_layout(qid, index, align=align, field=field)
    return jsonify(ok=True, align=align, index=index, field=field)


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
    field = _img_field(data)
    if field is None:
        return jsonify(ok=False, error="非法 field"), 400
    filestore.set_img_layout(qid, index, width=width, field=field)
    return jsonify(ok=True, width=width if width != "" else None,
                   index=index, field=field)


@app.route("/question/<qid>/img_split", methods=["POST"])
def question_img_split(qid):
    """设置题干图文分栏/解析图文混排模式（AJAX）。

    mode: "" 关 / "opts" 仅选项与图分栏 / "full" 整体题干与图分栏 /
    "sub" 仅小问与图分栏 / "between" 题干与选项（小问）之间 / "after" 题目后 /
    "pair" 四张图一图配一选项。

    **前置校验不能省**：模式与题型/正文结构不匹配时 render_body 会静默回退成普通
    渲染——库里存下了、按钮亮着、版式毫无变化，用户只看到「点了没反应」。所以宁可
    在写盘前拒绝并说清原因。`sub` 那条原因由 qrender 给（它知道是哪一条不满足）：
    路由自己拼通用文案会把「没有尾图」报成「没有分小问」，与事实相反。
    """
    data = request.get_json(silent=True) or {}
    mode = str(data.get("mode", "")).strip()
    field = _img_field(data)
    if field is None:
        return jsonify(ok=False, error="非法 field"), 400
    rec = filestore.get_question(qid)
    if not rec:
        return jsonify(ok=False, error="题目不存在"), 404
    if field == "solution":
        if mode not in ("", "full"):
            return jsonify(ok=False, error="解析只支持图文混排"), 400
        if mode == "full" and not _QIMG_RE.search(rec["solution"] or ""):
            return jsonify(ok=False, error="解析中没有图片，无法图文混排"), 400
        filestore.set_img_split(qid, mode, field="solution")
        body_html, groups = _html_of(qid, "solution")
        return jsonify(ok=True, mode=mode, field=field,
                       body_html=body_html, groups=groups)
    if mode and mode not in ("opts", "full", "sub", "between", "after", "pair"):
        return jsonify(ok=False, error="分栏模式不合法"), 400
    qtype = rec["type"]
    if mode and qtype not in ("单选题", "多选题", "填空题", "解答题"):
        return jsonify(ok=False, error="该题型不支持图文分栏"), 400
    if mode == "pair":
        if qtype not in ("单选题", "多选题"):
            return jsonify(ok=False, error="四图配选项仅选择题支持"), 400
        if not qrender.pair_applies(rec["body"] or "", qtype=qtype):
            return jsonify(
                ok=False,
                error="这道题不是「四个选项各配一张图」，无法一图配一选项"), 400
    if mode == "full" and qtype not in ("单选题", "多选题", "填空题", "解答题"):
        return jsonify(ok=False, error="该题型不支持整体图文分栏"), 400
    if mode == "opts" and qtype not in ("单选题", "多选题", "填空题"):
        return jsonify(ok=False, error="仅选项分栏只支持选择题；填空题旧数据按题干分栏兼容"), 400
    if mode == "sub":
        if qtype != "解答题":
            return jsonify(ok=False, error="小问分栏仅解答题支持"), 400
        reason = qrender.solve_split_reason(rec["body"] or "", qtype=qtype,
                                           img_split="sub")
        if reason:
            return jsonify(ok=False, error=reason), 400
    if mode == "between" and qtype not in ("单选题", "多选题", "解答题"):
        return jsonify(ok=False, error="题干与选项/小问之间仅选择题和解答题支持"), 400
    if mode == "after" and qtype not in ("单选题", "多选题", "填空题", "解答题"):
        return jsonify(ok=False, error="该题型不支持题后图片"), 400
    filestore.set_img_split(qid, mode, field="body")
    # 回渲好的正文，前端换掉 .body 即可，不必整页 reload——reload 会把同页其它题
    # 正在跑的 AI 重绘轮询一起杀掉（见 static/js/image-redraw.js 顶部）。
    # groups 一起回：切进/切出 pair 会改变连续图分组（配对模式下没有并排组）。
    body_html, groups = _html_of(qid, "body")
    return jsonify(ok=True, mode=mode, field=field,
                   body_html=body_html, groups=groups)


def _html_of(qid: str, field: str) -> tuple[str, list]:
    """重取题目并渲一遍正文（或解析），连带回「连续两图」分组。

    **必须在写盘之后重新取一次**：排版设置都是 render_body 的入参，用写盘前那份
    记录渲出来的还是改动前的版式。分组一起回是因为交换/堆叠都可能改变分组本身
    （交换后相邻关系不变但序号换了，堆叠改的是 stack 标记），前端拿它刷新按钮
    状态，不必自己推算一遍分组规则。
    """
    rec = filestore.get_question(qid)
    if not rec:
        return "", []
    if field == "solution":
        return str(qrender.render_solution(
            rec["solution"] or "",
            sol_img_layouts=rec["sol_img_layouts"],
            sol_img_split=rec["sol_img_split"])), qrender.fig_groups(
                rec["solution"] or "", None, rec["sol_img_layouts"],
                rec["sol_img_split"])
    return str(qrender.render_body(
        rec["body"] or "", rec["type"], img_layouts=rec["img_layouts"],
        img_width=rec["img_width"], img_align=rec["img_align"],
        img_split=rec["img_split"])), qrender.fig_groups(
            rec["body"] or "", rec["type"], rec["img_layouts"],
            rec["img_split"])


@app.route("/question/<qid>/img_stack", methods=["POST"])
def question_img_stack(qid):
    """多图组：左右排列（默认）↔ 上下排列。field="solution" 时作用于解析图片。

    stack 标记存在 img_layouts 里该组**首图**的条目上（filestore.set_img_layout
    的 stack 参数），所以前端传组内任一张图的序号都行——这里按 plan_figs 的分组
    归到首图。
    """
    data = request.get_json(silent=True) or {}
    index = _img_index(data)
    if index is None:
        return jsonify(ok=False, error="图片序号不合法"), 400
    field = _img_field(data)
    if field is None:
        return jsonify(ok=False, error="field 不合法"), 400
    stack = bool(data.get("stack"))
    rec = filestore.get_question(qid)
    if not rec:
        return jsonify(ok=False, error="题目不存在"), 404
    text = rec["solution"] if field == "solution" else rec["body"]
    layouts = rec["sol_img_layouts"] if field == "solution" else rec["img_layouts"]
    group = qrender.stack_group_of(
        text or "", index, layouts,
        rec["type"] if field == "body" else None,
        rec["img_split"] if field == "body" else rec["sol_img_split"])
    if not group:
        return jsonify(ok=False,
                       error="这张图不在多图组里，无法切换排列方向"), 400
    filestore.set_img_layout(qid, group[0], stack=stack, field=field)
    body_html, groups = _html_of(qid, field)
    return jsonify(ok=True, index=index, group=group, stack=stack, field=field,
                   body_html=body_html, groups=groups)


@app.route("/question/<qid>/img_swap", methods=["POST"])
def question_img_swap(qid):
    """交换正文里第 index、with 两张图的位置。field="solution" 时换解析里的两张。

    交换改的是**正文**（两个图引用互换），因此要同步换 img_layouts /
    img_originals 里的序号——序号是多处共享的不变量，见 filestore.swap_images。
    走那个函数而不是各自写盘，正文与两张表因此一定同进同出。
    """
    data = request.get_json(silent=True) or {}
    index = _img_index(data)
    other = _img_index({"index": data.get("with")})
    if index is None or other is None:
        return jsonify(ok=False, error="图片序号不合法"), 400
    if index == other:
        return jsonify(ok=False, error="两个序号相同，无需交换"), 400
    field = _img_field(data)
    if field is None:
        return jsonify(ok=False, error="field 不合法"), 400
    rec = filestore.get_question(qid)
    if not rec:
        return jsonify(ok=False, error="题目不存在"), 404
    text = rec["solution"] if field == "solution" else rec["body"]
    layouts = rec["sol_img_layouts"] if field == "solution" else rec["img_layouts"]
    group = qrender.stack_group_of(
        text or "", index, layouts,
        rec["type"] if field == "body" else None,
        rec["img_split"] if field == "body" else rec["sol_img_split"])
    group_lead = group[0] if group else None
    group_stacked = bool(next(
        (item.get("stack") for item in layouts
         if isinstance(item, dict) and item.get("i") == group_lead), False))
    new_text = qrender.swap_image_refs(text or "", index, other)
    if new_text is None:
        return jsonify(ok=False, error="图片序号越界"), 400
    filestore.swap_images(qid, index, other, new_text, field=field)
    # stack 是“这一组的方向”，不是某张图片的属性。交换涉及组首时，逐图元数据随
    # 图片换位后会把 stack 一并带走；这里把方向标记归回组首，拖动排序才不会顺带
    # 把上下排列改成左右排列。
    if group_lead is not None and group_lead in (index, other):
        moved_to = other if group_lead == index else index
        filestore.set_img_layout(qid, moved_to, stack=False, field=field)
        filestore.set_img_layout(qid, group_lead, stack=group_stacked, field=field)
    body_html, groups = _html_of(qid, field)
    return jsonify(ok=True, index=index, swapped_with=other, field=field,
                   body_html=body_html, groups=groups)


# ---------------------------------------------------------------------------
# AI 重绘配图（多模态模型看图 → TikZ → pdf+svg）
#
# 生成要跑一次视觉模型调用加一次 xelatex 编译，几十秒量级，不能占着请求线程；
# 故 POST 立刻返回 job_id，前端轮询 status。与转换任务共用同一套"内存字典 + 锁"
# 的做法（本地单用户，关服务即丢，无需 Celery）。
# ---------------------------------------------------------------------------

# job_id -> {status: pending|done|error, result: dict|None, error: str|None, ts: float}
_redraw_jobs: dict[str, dict] = {}
_redraw_jobs_lock = threading.Lock()
# done/error 的任务留半小时，够前端轮到 + 用户回来点"应用"；新建任务时顺手清。
_REDRAW_JOB_TTL = 1800


def _sweep_redraw_jobs():
    """清掉过期的终态任务。**调用方须持锁**（_jobs_lock 不可重入）。"""
    now = time.time()
    for jid in [k for k, v in _redraw_jobs.items()
                if v["status"] in ("done", "error") and now - v["ts"] > _REDRAW_JOB_TTL]:
        _redraw_jobs.pop(jid, None)


def _redraw_body_html(rec: dict) -> str:
    """重绘应用/还原后回给前端的新题干 HTML。

    走的是与 `qbody` 过滤器完全同一条渲染路径——前端拿它整块替换掉 `.q-body`，
    如果两边渲染参数不一致，替换后卡片的版式会与刷新后不同，那种"刷新一下就变了"
    的差异极难查。
    """
    return str(qrender.render_body(
        rec["body"] or "", rec["type"],
        img_layouts=rec["img_layouts"], img_width=rec["img_width"],
        img_align=rec["img_align"], img_split=rec["img_split"]))


def _redraw_worker(job_id: str, qid: str, index: int, extra: str):
    """后台线程：生成重绘图，结果塞进 _redraw_jobs。

    裸 Exception 也要兜住——这是线程的最外层，漏出去的话任务永远停在 pending，
    前端会一直转圈到超时，用户拿不到任何错误信息。
    """
    try:
        result = tikz_redraw.redraw(qid, index, extra)
        with _redraw_jobs_lock:
            _redraw_jobs[job_id] = {"status": "done", "result": result,
                                    "error": None, "ts": time.time()}
    except tikz_redraw.RedrawError as e:
        with _redraw_jobs_lock:
            _redraw_jobs[job_id] = {"status": "error", "result": None,
                                    "error": str(e), "ts": time.time()}
    except Exception:
        logger.exception("重绘任务异常 job=%s qid=%s index=%d", job_id, qid, index)
        with _redraw_jobs_lock:
            _redraw_jobs[job_id] = {"status": "error", "result": None,
                                    "error": "重绘过程出现未预期的错误，详见服务端日志。",
                                    "ts": time.time()}


@app.route("/question/<qid>/redraw", methods=["POST"])
def question_redraw(qid):
    """发起重绘，立刻返回 job_id（不等结果）。"""
    data = request.get_json(silent=True) or {}
    index = _img_index(data)
    if index is None:
        return jsonify(ok=False, error="非法图片序号"), 400
    if not filestore.get_question(qid):
        return jsonify(ok=False, error="题目不存在"), 404
    extra = str(data.get("extra", "") or "").strip()[:2000]

    job_id = uuid.uuid4().hex[:12]
    with _redraw_jobs_lock:
        _sweep_redraw_jobs()
        _redraw_jobs[job_id] = {"status": "pending", "result": None,
                                "error": None, "ts": time.time()}
    threading.Thread(target=_redraw_worker, args=(job_id, qid, index, extra),
                     daemon=True).start()
    return jsonify(ok=True, job_id=job_id)


@app.route("/question/<qid>/redraw/status/<job_id>")
def question_redraw_status(qid, job_id):
    """轮询重绘进度。任务本身失败时**仍回 200**——查询这个动作是成功的，
    HTTP 层报错会让前端的 fetch 分支把它当网络故障处理，看不到真正的原因。"""
    with _redraw_jobs_lock:
        job = _redraw_jobs.get(job_id)
        snapshot = dict(job) if job else None
    if snapshot is None:
        return jsonify(ok=False, error="任务不存在或已过期"), 404
    out = {"ok": True, "status": snapshot["status"], "error": snapshot["error"]}
    if snapshot["status"] == "done":
        r = snapshot["result"]
        out["result"] = {
            "name": r["name"],
            "src": url_for("asset_serve", filename=r["name"]),
            "old": r["old"],
            "old_src": url_for("asset_serve", filename=r["old"]),
            "code": r["code"],
        }
    return jsonify(out)


@app.route("/question/<qid>/redraw/apply", methods=["POST"])
def question_redraw_apply(qid):
    """把预览里的那张图写进正文。name 必须过 validate_generated。"""
    data = request.get_json(silent=True) or {}
    index = _img_index(data)
    if index is None:
        return jsonify(ok=False, error="非法图片序号"), 400
    name = str(data.get("name", "") or "").strip()
    try:
        tikz_redraw.validate_generated(name)
        old = tikz_redraw.apply_redraw(qid, index, name)
    except tikz_redraw.RedrawError as e:
        return jsonify(ok=False, error=str(e)), 400
    rec = filestore.get_question(qid)
    return jsonify(ok=True, old=old, index=index,
                   body_html=_redraw_body_html(rec))


@app.route("/question/<qid>/redraw/restore", methods=["POST"])
def question_redraw_restore(qid):
    """退回原图。只收 index——原文件名一律从 frontmatter 读，不接受客户端传路径。"""
    data = request.get_json(silent=True) or {}
    index = _img_index(data)
    if index is None:
        return jsonify(ok=False, error="非法图片序号"), 400
    try:
        orig, _cur = tikz_redraw.restore_original(qid, index)
    except tikz_redraw.RedrawError as e:
        return jsonify(ok=False, error=str(e)), 400
    rec = filestore.get_question(qid)
    return jsonify(ok=True, orig=orig, index=index,
                   body_html=_redraw_body_html(rec))


@app.route("/clear", methods=["POST"])
def clear():
    filestore.clear_selected()
    if request.accept_mimetypes.best == "application/json":
        return jsonify(ok=True, count=0, message="已清空所有勾选")
    flash("已清空所有勾选", "ok")
    return redirect(request.referrer or url_for("index"))


@app.route("/delete_selected", methods=["POST"])
def delete_selected():
    """删除当前已勾选的题目（破坏性，移入回收站）。"""
    ids = filestore.selected_ids()
    deleted = []
    for qid in ids:
        if filestore.delete_question(qid):
            deleted.append(qid)
    if deleted:
        message = f"已删除 {len(deleted)} 道题"
        if request.accept_mimetypes.best == "application/json":
            return jsonify(ok=True, deleted=deleted, count=filestore.count_selected(),
                           message=message)
        flash(message, "ok")
    else:
        if request.accept_mimetypes.best == "application/json":
            return jsonify(ok=False, error="没有勾选任何题目"), 400
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
    explicit_all = request.form.get("all") in ("1", "true", "on")
    # 根页本身不代表“全部题目”。局部切换若因前端旧状态漏传 collection，过去会
    # 静默退化成全库全选；只有用户明确进入“全部题目”（all=1），或确有筛选/
    # 文件夹范围时才允许执行。前端同步失效也只能得到明确错误，不能扩大作用域。
    has_explicit_scope = bool(
        collection_id or explicit_all or tags or type_ or difficulty
        or search or starred_only
    )
    if not has_explicit_scope:
        message = "当前页面没有明确的题目范围，请先选择文件夹或“全部题目”"
        if request.accept_mimetypes.best == "application/json":
            return jsonify(ok=False, error=message), 400
        flash(message, "err")
        return redirect(request.referrer or url_for("index"))
    # 单卷/年份视图的“全选”只需扫描这一棵目录。旧实现先 `_all_records()` 再按
    # folder 前缀过滤，选一份 20 题试卷也会解析 1.3 万题，与局部加载背道而驰。
    records = (filestore.collection_records_snapshot(collection_id)
               if collection_id else None)
    rows = filestore.list_questions(tags=tags, match=match, qtype=type_,
                                    difficulty=difficulty, search=search,
                                    starred=starred_only,
                                    collection=collection_id, records=records)
    filestore.select_ids([r["id"] for r in rows])
    message = f"已全选 {len(rows)} 道题"
    if request.accept_mimetypes.best == "application/json":
        return jsonify(ok=True, count=filestore.count_selected(),
                       matched=len(rows), message=message)
    flash(message, "ok")
    return redirect(request.referrer or url_for("index"))


@app.route("/tags/<name>/rename", methods=["POST"])
def tag_rename(name):
    """标签改名（新名已存在则合并）。name 是旧标签名本身。"""
    new_name = request.form.get("name", "").strip()
    if not new_name:
        if request.accept_mimetypes.best == "application/json":
            return jsonify(ok=False, error="新标签名不能为空"), 400
        flash("新标签名不能为空", "err")
        return redirect(request.referrer or url_for("index"))
    filestore.rename_tag(name, new_name)
    message = f"标签已改名为「{new_name}」" if name != new_name else "未改动"
    if request.accept_mimetypes.best == "application/json":
        return jsonify(ok=True, old_name=name, new_name=new_name,
                       message=message)
    flash(message, "ok")
    return redirect(request.referrer or url_for("index"))


@app.route("/tag_selected", methods=["POST"])
def tag_selected():
    """给当前已勾选的题批量追加标签。"""
    tag_names = [t.strip() for t in request.form.get("tags", "").split(",") if t.strip()]
    if not tag_names:
        if request.accept_mimetypes.best == "application/json":
            return jsonify(ok=False, error="请填写要添加的标签"), 400
        flash("请填写要添加的标签", "err")
        return redirect(request.referrer or url_for("index"))
    ids = filestore.selected_ids()
    if not ids:
        if request.accept_mimetypes.best == "application/json":
            return jsonify(ok=False, error="请先勾选题目"), 400
        flash("请先勾选题目", "err")
    else:
        filestore.add_tags_to(ids, tag_names)
        message = f"已给 {len(ids)} 道题添加标签：{'、'.join(tag_names)}"
        if request.accept_mimetypes.best == "application/json":
            return jsonify(ok=True, ids=ids, tags=tag_names,
                           count=filestore.count_selected(), message=message)
        flash(message, "ok")
    return redirect(request.referrer or url_for("index"))


@app.route("/difficulty_selected", methods=["POST"])
def difficulty_selected():
    """给当前已勾选的题批量设置难度（level 为 1-5 或 '' 清除）。"""
    level = request.form.get("level", "").strip()
    if level and level not in ("1", "2", "3", "4", "5"):
        if request.accept_mimetypes.best == "application/json":
            return jsonify(ok=False, error="难度须为 1-5"), 400
        flash("难度须为 1-5", "err")
        return redirect(request.referrer or url_for("index"))
    ids = filestore.selected_ids()
    if not ids:
        if request.accept_mimetypes.best == "application/json":
            return jsonify(ok=False, error="请先勾选题目"), 400
        flash("请先勾选题目", "err")
    else:
        for qid in ids:
            filestore.set_difficulty(qid, level)
        label = f"难度 {level}" if level else "清除难度"
        message = f"已给 {len(ids)} 道题设置：{label}"
        if request.accept_mimetypes.best == "application/json":
            return jsonify(ok=True, ids=ids, level=level,
                           count=filestore.count_selected(), message=message)
        flash(message, "ok")
    return redirect(request.referrer or url_for("index"))


# ---------------------------------------------------------------------------
# 题集（= 文件夹）
# ---------------------------------------------------------------------------


def _selected_ids() -> list[str]:
    """当前已勾选的题 id 列表。"""
    return filestore.selected_ids()


@app.route("/collections/create", methods=["POST"])
def collection_create():
    name = request.form.get("name", "").strip()
    parent_id = request.form.get("parent_id", "")
    if not name:
        if request.accept_mimetypes.best == "application/json":
            return jsonify(ok=False, error="题集名不能为空"), 400
        flash("题集名不能为空", "err")
    else:
        try:
            cid = filestore.create_collection(name, parent_id=parent_id)
            message = f"已新建题集「{name}」"
            if request.accept_mimetypes.best == "application/json":
                return jsonify(ok=True, id=cid, name=name, message=message)
            flash(message, "ok")
        except ValueError as e:
            if request.accept_mimetypes.best == "application/json":
                return jsonify(ok=False, error=str(e)), 400
            flash(str(e), "err")
    return redirect(request.referrer or url_for("index"))


@app.route("/collections/children")
def collection_children():
    """文件夹树按需展开：只返回某一级，不触发整库题目扫描。"""
    try:
        children = filestore.list_collection_children(
            (request.args.get("parent") or "").strip("/"))
    except ValueError as exc:
        return jsonify(ok=False, error=str(exc)), 400
    return jsonify(ok=True, children=children)


@app.route("/collections/options")
def collection_options():
    """低频的完整文件夹下拉选项，首次操作时才递归建立。"""
    tree = filestore.list_collections_tree([])
    rows = [{"id": row["id"], "name": row["name"], "depth": row["depth"]}
            for row in filestore.all_collections(tree)]
    return jsonify(ok=True, collections=rows)


@app.route("/collections/<path:cid>/rename", methods=["POST"])
def collection_rename(cid):
    new_name = request.form.get("name", "").strip()
    if not new_name:
        if request.accept_mimetypes.best == "application/json":
            return jsonify(ok=False, error="题集名不能为空"), 400
        flash("题集名不能为空", "err")
    else:
        try:
            new_id = filestore.rename_collection(cid, new_name)
            message = f"已改名为「{new_name}」"
            if request.accept_mimetypes.best == "application/json":
                return jsonify(ok=True, id=new_id, name=new_name,
                               message=message)
            flash(message, "ok")
        except ValueError as e:
            if request.accept_mimetypes.best == "application/json":
                return jsonify(ok=False, error=str(e)), 400
            flash(str(e), "err")
    return redirect(request.referrer or url_for("index"))


@app.route("/collections/<path:cid>/move", methods=["POST"])
def collection_move(cid):
    """把文件夹 cid 移到新的父级下（AJAX），parent_id 为空表示移到顶级。"""
    data = request.get_json(silent=True) or {}
    new_parent = data.get("parent_id") or ""
    try:
        new_id = filestore.move_folder(cid, new_parent)
        return jsonify(ok=True, id=new_id, message="文件夹已移动")
    except ValueError as e:
        return jsonify(ok=False, error=str(e)), 400


@app.route("/collections/<path:cid>/delete", methods=["POST"])
def collection_delete(cid):
    deleted = filestore.get_collection(cid)
    filestore.delete_collection(cid)
    if request.accept_mimetypes.best == "application/json":
        if not deleted:
            return jsonify(ok=False, error="题集不存在"), 404
        parent_id = PurePosixPath(cid).parent.as_posix()
        if parent_id == ".":
            parent_id = ""
        return jsonify(ok=True, id=cid, parent_id=parent_id,
                       message="题集已移入回收站")
    flash("已移入回收站", "ok")
    # 若正停留在被删题集视图，回到全部
    return redirect(url_for("index"))


@app.route("/collections/<path:cid>/add", methods=["POST"])
def collection_add(cid):
    """把当前已勾选的题加入题集。"""
    ids = _selected_ids()
    if not ids:
        if request.accept_mimetypes.best == "application/json":
            return jsonify(ok=False, error="请先勾选题目再加入题集"), 400
        flash("请先勾选题目再加入题集", "err")
    else:
        col = filestore.get_collection(cid)
        if not col:
            if request.accept_mimetypes.best == "application/json":
                return jsonify(ok=False, error="题集不存在"), 404
            flash("题集不存在", "err")
            return redirect(request.referrer or url_for("index"))
        for qid in ids:
            filestore.add_to_collection(qid, cid)
        message = f"已把 {len(ids)} 道题加入「{col['name']}」"
        if request.accept_mimetypes.best == "application/json":
            return jsonify(ok=True, ids=ids, count=filestore.count_selected(),
                           message=message)
        flash(message, "ok")
    return redirect(request.referrer or url_for("index"))


@app.route("/collections/<path:cid>/add_one", methods=["POST"])
def collection_add_one(cid):
    """把一道题放进文件夹（AJAX，拖题进文件夹用）。

    与上面的 `/add` 区别只在数据来源：那个收的是勾选篮里的一批，这个收的是
    拖拽带来的单个 question_id。软件版一题只能在一个目录下（见
    filestore.add_to_collection），所以这个动作是**移动**而不是「加一份引用」。
    """
    qid = (request.get_json(silent=True) or {}).get("question_id")
    qid = str(qid or "").strip()
    if not qid:
        return jsonify(ok=False, error="题目 id 无效"), 400
    col = filestore.get_collection(cid)
    if not col:
        return jsonify(ok=False, error="题集不存在"), 404
    if not filestore.get_question(qid):
        return jsonify(ok=False, error="题目不存在"), 404
    filestore.add_to_collection(qid, cid)
    return jsonify(ok=True)


@app.route("/collections/<path:cid>/remove", methods=["POST"])
def collection_remove(cid):
    """把当前已勾选的题移出题集（题目本身保留）。"""
    ids = _selected_ids()
    if not ids:
        if request.accept_mimetypes.best == "application/json":
            return jsonify(ok=False, error="请先勾选要移出的题目"), 400
        flash("请先勾选要移出的题目", "err")
    else:
        for qid in ids:
            filestore.remove_from_collection(qid, cid)
        message = f"已把 {len(ids)} 道题移出本集"
        if request.accept_mimetypes.best == "application/json":
            return jsonify(ok=True, ids=ids, count=filestore.count_selected(),
                           message=message)
        flash(message, "ok")
    return redirect(request.referrer or url_for("index"))


# ---------------------------------------------------------------------------
# 文件夹原卷附件面板（查看 / 上传 / 删除 / 重新转换）
#
# 与服务器版 routes/questions.py 的 folder_paper_* 一一对应，差别只在身份：那边
# 附件是 collection_papers 表的自增 id、每个请求都要校 owner；本地没有表也没有
# 用户，附件身份就是**相对题库根的路径**，一律经 filestore.paper_abspath 反查并
# 验祖先关系。软件版无鉴权，这层路径校验是唯一防线，不许绕。
# ---------------------------------------------------------------------------

# 允许上传的原卷格式。**不是安全校验**（浏览器给的 Content-Type 与扩展名都能伪造），
# 只是挡住明显选错的文件；真正的边界是「落点固定在题库目录内、文件名经
# filestore.paper_filename 清洗」。软件版只监听 127.0.0.1、单人本机使用，没有
# 服务器版 upload_guard 的多用户配额那一层需求。
# 这里比 config.EXAM_EXTS 多收 `.doc`：原卷附件是**存档**，不经 pandoc 转换，
# 存进去只为在 Obsidian 里能点开原件，所以 .doc 读不了这件事在这条路上不成立。
_PAPER_UPLOAD_EXTS = config.EXAM_EXTS | {".doc"}
_PAPER_MAX_BYTES = config.MAX_EXAM_DOCUMENT_BYTES


@app.route("/collections/<path:cid>/papers/upload", methods=["POST"])
def folder_paper_upload(cid):
    """往文件夹里手动补一份原卷。"""
    if not filestore.get_collection(cid):
        flash("题集不存在", "err")
        return redirect(url_for("index"))
    file = request.files.get("file")
    if not file or not file.filename:
        flash("未选择文件", "err")
        return redirect(request.referrer or url_for("index"))
    if Path(file.filename).suffix.lower() not in _PAPER_UPLOAD_EXTS:
        flash("不支持的文件格式", "err")
        return redirect(request.referrer or url_for("index"))
    ext = Path(file.filename).suffix.lower()
    try:
        if ext == ".doc":
            pos = file.stream.tell()
            file.stream.seek(0)
            head = file.stream.read(8)
            file.stream.seek(pos)
            if head != b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
                raise _UploadRejected("扩展名是 .doc，但内容不是旧版 Word 文档")
            if _file_size(file) > _PAPER_MAX_BYTES:
                raise _UploadRejected(
                    f"文件过大（上限 {_PAPER_MAX_BYTES // (1024 * 1024)}MB）")
        else:
            _check_exam_file(file)
    except _UploadRejected as exc:
        flash(str(exc), "err")
        return redirect(request.referrer or url_for("index"))
    kind = "solution" if request.form.get("kind") == "solution" else "exam"
    # 先落到临时目录再走 store_paper：那个函数是本地唯一的「原卷落盘」出口，
    # 撞名加后缀、文件名清洗都在里面，绕过它就得再抄一份同样的逻辑。
    config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    tmp = config.UPLOAD_DIR / f"{uuid.uuid4().hex}{Path(file.filename).suffix}"
    try:
        file.save(str(tmp))
        if tmp.stat().st_size > _PAPER_MAX_BYTES:
            flash(f"文件过大（上限 "
                  f"{_PAPER_MAX_BYTES // (1024 * 1024)}MB）", "err")
            return redirect(request.referrer or url_for("index"))
        filestore.store_paper(str(tmp), cid, file.filename, kind)
        flash(f"已保存原卷「{file.filename}」", "ok")
    except (OSError, ValueError) as e:
        flash(f"原卷保存失败：{e}", "err")
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
    return redirect(request.referrer or url_for("index"))


@app.route("/papers/view")
def folder_paper_view():
    """在浏览器里打开原卷（`?download=1` 则当附件下载）。

    路径经 `paper_abspath` 反查，越界一律 404。**不用 as_attachment 做默认**：
    插件是 Electron iframe，`as_attachment` 触发的下载在那里毫无反应（同
    `/outfile/<token>` 那条经验），默认内联打开才有用。
    """
    target = filestore.paper_abspath(request.args.get("id", ""))
    if target is None:
        abort(404)
    download = request.args.get("download") in ("1", "true")
    return send_file(str(target), as_attachment=download,
                     download_name=target.name)


@app.route("/papers/delete", methods=["POST"])
def folder_paper_delete():
    """彻底删除一份原卷（不进回收站，面板上的按钮已写明）。"""
    paper_id = request.form.get("id", "")
    name = Path(paper_id.replace("\\", "/")).name or "原卷"
    if filestore.remove_paper(paper_id):
        flash(f"已删除原卷「{name}」", "ok")
    else:
        flash("原卷不存在", "err")
    return redirect(request.referrer or url_for("index"))


@app.route("/collections/<path:cid>/papers/reconvert", methods=["POST"])
def folder_paper_reconvert(cid):
    """拿文件夹里已存的原卷再跑一遍识别，题目自动落回**这个**文件夹。

    与批量上传的唯一区别是 `target_folder_id`：它让 `_auto_import_folder` 短路到
    本文件夹（不新建、不看 pack/per_task 开关），也让两处清理点放过这两个文件
    ——它们是题库里的原卷，不是临时上传件（见 `_maybe_finish_batch`）。
    """
    if not filestore.get_collection(cid):
        flash("题集不存在", "err")
        return redirect(url_for("index"))
    exam = filestore.paper_abspath(request.form.get("exam_paper_id", ""))
    if exam is None:
        flash("请先勾选一份「题干」原卷", "err")
        return redirect(request.referrer or url_for("index"))
    sol_id = request.form.get("solution_paper_id", "")
    sol = filestore.paper_abspath(sol_id) if sol_id else None
    if sol_id and sol is None:
        flash("勾选的解析卷不存在", "err")
        return redirect(request.referrer or url_for("index"))
    ocr_backend = _parse_ocr_backend(request.form.get("ocr_backend", ""))

    disp_name = exam.name
    if sol is not None:
        disp_name = f"{Path(disp_name).stem} + {sol.name}（解析）"
    job_id = uuid.uuid4().hex
    with _jobs_lock:
        _jobs[job_id] = {"status": "pending", "md": None, "error": None,
                         "filename": exam.name, "path": str(exam)}
        _persist_job(job_id, _jobs[job_id])
    grp = {
        "gid": 0, "job_id": job_id, "file_path": str(exam),
        "solution_path": str(sol) if sol is not None else None,
        "include_solution": sol is not None,
        "only_numbers": None, "filename": disp_name,
        "engine": _DEFAULT_ENGINE, "ocr_backend": ocr_backend,
        "block_mode": _parse_block_mode(""),
        "num_template": "",
        "cleanup_paths": [],    # 不删原卷文件——它们在题库目录里，不是临时上传件
        "status": "pending", "md": None, "error": None,
        "pending": None, "note": "",
        "reviewed": None, "imported_count": 0,
    }
    batch_id = uuid.uuid4().hex
    with _batch_jobs_lock:
        _batch_jobs[batch_id] = {
            "status": "converting", "groups": [grp], "current_idx": 0,
            "files_cleaned": False, "created_at": time.time(),
            "running": 0, "cancelled": False,
            "pack_folder_name": "", "auto_import": False,
            "per_task_folder": False, "auto_keep_original": False,
            # 直指当前文件夹——这是原卷面板重新转换与普通批量上传的唯一区别
            "target_folder_id": cid,
        }
        _persist_batch(batch_id, _batch_jobs[batch_id])
    threading.Thread(target=_convert_batch_worker, args=(batch_id,),
                     daemon=True).start()
    flash("已开始安全重跑。识别结果只进入审核页，不会自动覆盖或追加到现有题目；"
          "可从导航栏「转换中」查看进度", "ok")
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

# 实时执行仍在内存；每次状态变化另存 task_store JSON 快照，插件/后端重启后恢复。
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
# 左侧 PDF 对照 iframe 零改动复用。稳定状态另存 task_store，关服务不再丢。
_batch_jobs: dict[str, dict] = {}
_batch_jobs_lock = threading.Lock()

_RESTART_INTERRUPTED = ("后端重启时任务尚未完成；为避免重复调用和重复计费，"
                        "请手动重新转换")
_CANCEL_UNSTARTED = "已中止（未开始转换）"
_CANCEL_INFLIGHT = "已中止（额度已消耗，结果作废）"


def _persist_job(job_id: str, job: dict) -> None:
    task_store.save("job", job_id, job)


def _persist_batch(batch_id: str, batch: dict) -> None:
    task_store.save("batch", batch_id, batch)


def restore_persisted_tasks() -> None:
    """恢复稳定结果；不确定是否已经计费的在途调用只标错、不自动重放。"""
    restored_jobs = {}
    changed_jobs = []
    for job_id, job in task_store.load("job"):
        if job.get("status") in ("pending", "converting"):
            job["status"] = "error"
            job["error"] = _RESTART_INTERRUPTED
            changed_jobs.append((job_id, job))
        restored_jobs[job_id] = job

    restored_batches = {}
    changed_batches = []
    for batch_id, batch in task_store.load("batch"):
        if not batch.get("created_at"):
            batch["created_at"] = 0
        groups = batch.get("groups") or []
        changed = False
        for group in groups:
            if group.get("in_flight"):
                group["in_flight"] = False
                changed = True
            if group.get("status") in ("pending", "converting"):
                group["status"] = "error"
                group["error"] = _RESTART_INTERRUPTED
                changed = True
        conv_done = all(g.get("status") in ("done", "error", "awaiting_block_review")
                        for g in groups)
        wanted = "done" if conv_done else "converting"
        if batch.get("status") != wanted or batch.get("running"):
            changed = True
        batch["status"] = wanted
        batch["running"] = 0
        # 整批中止标记是上一进程的执行控制位；各组自己的 cancelled 要保留，
        # 这样用户仍能辨认并重新转换那一组。
        batch["cancelled"] = False
        if changed:
            changed_batches.append((batch_id, batch))
        restored_batches[batch_id] = batch

    # 结构合集展开时先发布含 N 个子组的 batch，再补各自 job 快照。
    # 虽然每个 JSON 文件自身是原子写，两类快照之间仍可能恰好断电。批次是
    # 用户可见真源；若其中一组缺 job，按组字段补最小快照，保住原卷预览与校对。
    for batch in restored_batches.values():
        for group in batch.get("groups") or []:
            job_id = group.get("job_id")
            if not job_id or job_id in restored_jobs:
                continue
            job = {
                "status": group.get("status") or "error",
                "md": group.get("md"),
                "error": group.get("error"),
                "filename": group.get("filename") or "转换任务",
                "path": group.get("file_path"),
                "solution_path": group.get("solution_path"),
                "ocr_backend": _parse_ocr_backend(
                    group.get("ocr_backend", "")),
            }
            restored_jobs[job_id] = job
            changed_jobs.append((job_id, job))

    with _jobs_lock:
        _jobs.clear()
        _jobs.update(restored_jobs)
    with _batch_jobs_lock:
        _batch_jobs.clear()
        _batch_jobs.update(restored_batches)

    for job_id, job in changed_jobs:
        _persist_job(job_id, job)
    for batch_id, batch in changed_batches:
        _persist_batch(batch_id, batch)


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


# 缺省识别引擎。放成模块常量而不是在三处各写一遍 `or converter.ENGINE_WHOLE`：
# 翻默认值时漏改一处，表现是「有的入口翻了有的没翻」，而这两条路径的产物长得一样，
# 用户只会看到「同一份卷子两次结果不同」。与线上版 routes/import_convert.py
# 的 _DEFAULT_ENGINE 逐字对齐——两版的识别口径必须完全一致（见下面 _parse_engine）。
_DEFAULT_ENGINE = converter.ENGINE_BLOCK
_DEFAULT_OCR_BACKEND = converter.OCR_MINERU


def _parse_ocr_backend(raw: str) -> str:
    """OCR 服务选择。不认识的值回落 MinerU，保持旧任务和旧页面行为不变。"""
    return converter.normalize_ocr_backend(raw or _DEFAULT_OCR_BACKEND)


def _parse_engine(raw: str) -> str:
    """表单里的识别引擎选择。只认显式的 "whole"，其余（含缺省）一律走逐块路径。

    默认值 2026-08-08 从 whole 翻成 block，与线上版（`69d03d8`）对齐。原先的保守
    口径（「新路径是加出来的第二条路，不能因为参数名写错就换掉默认行为」）在逐块
    路径还没有回归手段时是对的；线上版翻默认的理由同样适用于本地版：整篇路径的
    块数由模型决定，漏题时账面完全正常（v0.3.1 的 `. .....4分` 吞掉一整道题就是
    这个形状）。whole 保留为可选兜底，不删。

    **两版默认值必须一致**：只有逐块路径会跑 `mechfix.normalize_block`，而它最后
    一步 `re.sub(r"\\n{3,}", "\\n\\n", ...)` 是唯一收敛连续空行的地方。默认值分叉的
    直接症状就是本地版入库的解答题小问之间空三行、线上版不空——同一份卷子两版
    产物不同，正是这个函数的默认值造成的。
    """
    return (converter.ENGINE_WHOLE if (raw or "").strip() == "whole"
            else _DEFAULT_ENGINE)


# 逐题识别切完块之后怎么处理：直接送 AI / 先人工审拆题 / 完全不送 AI
_BLOCK_MODE_ALL_AI = "all_ai"
_BLOCK_MODE_MANUAL = "manual"
_BLOCK_MODE_NO_AI = "no_ai"
_BLOCK_MODES = (_BLOCK_MODE_ALL_AI, _BLOCK_MODE_MANUAL, _BLOCK_MODE_NO_AI)


def _parse_block_mode(raw: str) -> str:
    """表单里的拆题处理方式。同 _parse_engine：不认识的值一律落回默认。"""
    val = (raw or "").strip()
    return val if val in _BLOCK_MODES else _BLOCK_MODE_NO_AI


def _parse_num_template(raw: str) -> str:
    """题号模板。空串=自动判定；非空则先编译一遍验证写法，编译结果丢掉——
    真正切块时 blocksplit 会自己再编一次，这里只为把错误挡在提交那一刻。
    """
    tpl = (raw or "").strip()
    if not tpl:
        return ""
    blocksplit.compile_dialect(tpl)   # 写法不合法会抛 TemplateError
    return tpl


def _convert_with_ocr_credentials(ocr_backend: str, make_call):
    """转换层会按每个 OCR 文档任务取凭证；这里只保留统一调用签名。"""
    _parse_ocr_backend(ocr_backend)
    return make_call("", "")


def _convert_worker(job_id: str, saved_path, orig_filename: str,
                    include_solution: bool = False, solution_path=None,
                    only_numbers=None, provider=None,
                    engine: str = _DEFAULT_ENGINE,
                    num_template: str = "",
                    ocr_backend: str = _DEFAULT_OCR_BACKEND):
    """后台线程：跑转换，结果写回 _jobs。

    solution_path 非空 → 走「题干+解析双文件」路径，按题号关联解析。
    only_numbers 非空 → 仅导入指定题号的题（压轴题过滤）。
    provider 在起线程前就解析好传进来（这里没有请求上下文）。
    上传文件不在此删除——保留供预览对照，由下次转换前 _clean_uploads 清理。
    """
    notes: list[str] = []
    try:
        if solution_path is not None:
            md = _convert_with_ocr_credentials(
                ocr_backend,
                lambda tok, doc2x_key: converter.convert_exam_and_solution(
                    saved_path, solution_path, mineru_token=tok,
                    only_numbers=only_numbers,
                    provider=provider, engine=engine, num_template=num_template,
                    note_sink=notes.append, ocr_backend=ocr_backend,
                    doc2x_api_key=doc2x_key))
        else:
            md = _convert_with_ocr_credentials(
                ocr_backend,
                lambda tok, doc2x_key: converter.convert_file(
                    saved_path, mineru_token=tok,
                    include_solution=include_solution,
                    only_numbers=only_numbers, provider=provider, engine=engine,
                    num_template=num_template, note_sink=notes.append,
                    ocr_backend=ocr_backend, doc2x_api_key=doc2x_key))
        with _jobs_lock:
            # 池子重试会把整条转换重跑一遍，notes 里可能攒了同一句话两份，去重
            _jobs[job_id].update(status="done", md=md,
                                 note=" ".join(dict.fromkeys(notes)))
            _persist_job(job_id, _jobs[job_id])
    except Exception as e:
        with _jobs_lock:
            _jobs[job_id].update(status="error", error=str(e))
            _persist_job(job_id, _jobs[job_id])


def _convert_one_group(batch_id: str, g: dict):
    """转换一组。三条分支：

    - 逐题识别 + 拆题人工审核（block_mode=manual）：只切块，停在
      awaiting_block_review，等用户在拆题审核页确认后才继续；
    - 逐题识别 + 完全不送 AI（no_ai）：切块后立刻机械渲染收尾，落到 done；
    - 其余（整篇识别，或逐题识别直接送 AI）：走 convert_file /
      convert_exam_and_solution 老路径。

    某组抛异常只标记该组 error，不影响同批其他组。
    """
    job_id = g["job_id"]
    with _batch_jobs_lock:
        batch = _batch_jobs.get(batch_id)
        if not batch or g not in batch.get("groups", []):
            return
        attempt = int(g.get("attempt") or 0)
        is_collection_parent = (
            g.get("collection_strategy") == "ocr_structure"
            and not g.get("collection_unit"))
        # 父组必须重走“整本 OCR → 展开”阶段，不能进 _convert_one_group
        # 被当成一张普通卷子。正常由 batch worker 的展开阶段消费；竞态下
        # 即使误调到这里也只保留 pending，绝不能把两本整集送进普通切题链。
        if is_collection_parent:
            g["status"] = "pending"
            _persist_batch(batch_id, batch)
            return
        g["status"] = "converting"
        _persist_batch(batch_id, batch)
    with _jobs_lock:
        if job_id in _jobs:
            _jobs[job_id]["status"] = "converting"
            _persist_job(job_id, _jobs[job_id])
    # 每组转换前现查一次：整批可能跑很久，中途在设置页换了模型/Token 应当生效
    provider = providers.resolve_active()
    # 缺键时按当前默认（_DEFAULT_ENGINE）走，不按「批次创建时的默认」走：那个
    # 值没存下来，硬编码一个旧默认会让老批次和新批次口径分叉。
    engine = g.get("engine") or _DEFAULT_ENGINE
    ocr_backend = _parse_ocr_backend(g.get("ocr_backend", ""))
    num_template = g.get("num_template") or ""
    block_mode = g.get("block_mode") or _BLOCK_MODE_NO_AI
    with _batch_jobs_lock:
        batch = _batch_jobs.get(batch_id)
        auto_import = bool(batch and batch.get("auto_import"))
    # 勾了「整批免审」就是要求全自动，不能停下来等人；no_ai 只是不送 LLM，与免审
    # 不冲突（照样能自动入库），所以只有 manual 被免审否决。
    if auto_import and block_mode == _BLOCK_MODE_MANUAL:
        block_mode = _BLOCK_MODE_ALL_AI
    # 切块阶段的告警（选项只剩标签之类）攒在这里，四条分支都往里写，收尾时落到
    # g["note"] 供校对页显示。这类丢失过了 LLM 那一步就再没人能发现，不显示等于没检测。
    notes: list[str] = []
    collection_raw_path = g.get("collection_raw_path")
    try:
        if engine == converter.ENGINE_BLOCK and block_mode != _BLOCK_MODE_ALL_AI:
            if collection_raw_path:
                # 结构合集已在父任务中整本 OCR 完成；子组只读自己的
                # raw Markdown，不会为每组重传一次百页 PDF。
                pending = converter.convert_collection_unit_to_blocks(
                    collection_raw_path,
                    num_template=num_template,
                    only_numbers=g["only_numbers"],
                    note_sink=notes.append,
                    source_name=g.get("filename") or "合集单元",
                    source_pdf=g.get("file_path"),
                    ocr_backend=ocr_backend,
                    ocr_meta=g.get("collection_ocr_meta") or {})
            elif g["solution_path"] is not None:
                pending = _convert_with_ocr_credentials(
                    ocr_backend,
                    lambda tok, doc2x_key: converter.convert_exam_and_solution_to_blocks(
                        g["file_path"], g["solution_path"], mineru_token=tok,
                        num_template=num_template, only_numbers=g["only_numbers"],
                        note_sink=notes.append, ocr_backend=ocr_backend,
                        doc2x_api_key=doc2x_key))
            else:
                pending = _convert_with_ocr_credentials(
                    ocr_backend,
                    lambda tok, doc2x_key: converter.convert_file_to_blocks(
                        g["file_path"], mineru_token=tok,
                        num_template=num_template,
                        only_numbers=g["only_numbers"], note_sink=notes.append,
                        ocr_backend=ocr_backend, doc2x_api_key=doc2x_key))
            if block_mode == _BLOCK_MODE_MANUAL:
                # 停在这里等人工审拆题结果。pending 留着，供审核页渲染。
                with _batch_jobs_lock:
                    batch = _batch_jobs.get(batch_id)
                    if (not batch or g not in batch.get("groups", [])
                            or g.get("cancelled")
                            or int(g.get("attempt") or 0) != attempt):
                        return
                    g["pending"] = pending
                    g["status"] = "awaiting_block_review"
                    g["note"] = " ".join(dict.fromkeys(notes))
                    batch = _batch_jobs.get(batch_id)
                    if batch:
                        _persist_batch(batch_id, batch)
                with _jobs_lock:
                    if job_id in _jobs:
                        _jobs[job_id]["status"] = "awaiting_block_review"
                        _persist_job(job_id, _jobs[job_id])
                return
            # no_ai：不花额度，机械渲染直接收尾
            md = converter.finish_block_review(
                pending, action="skip",
                include_solution=g["include_solution"],
                note_sink=notes.append)
        elif collection_raw_path:
            md = converter.convert_collection_unit(
                collection_raw_path,
                include_solution=g["include_solution"],
                only_numbers=g["only_numbers"],
                provider=provider, engine=engine,
                num_template=num_template, note_sink=notes.append,
                source_name=g.get("filename") or "合集单元",
                ocr_backend=ocr_backend,
                ocr_meta=g.get("collection_ocr_meta") or {})
        elif g["solution_path"] is not None:
            md = _convert_with_ocr_credentials(
                ocr_backend,
                lambda tok, doc2x_key: converter.convert_exam_and_solution(
                    g["file_path"], g["solution_path"], mineru_token=tok,
                    only_numbers=g["only_numbers"],
                    provider=provider, engine=engine, num_template=num_template,
                    note_sink=notes.append, ocr_backend=ocr_backend,
                    doc2x_api_key=doc2x_key))
        else:
            md = _convert_with_ocr_credentials(
                ocr_backend,
                lambda tok, doc2x_key: converter.convert_file(
                    g["file_path"], mineru_token=tok,
                    include_solution=g["include_solution"],
                    only_numbers=g["only_numbers"],
                    provider=provider, engine=engine, num_template=num_template,
                    note_sink=notes.append, ocr_backend=ocr_backend,
                    doc2x_api_key=doc2x_key))
        with _batch_jobs_lock:
            # 中止发生在外部调用期间时，那次额度已经花掉，但用户明确要求放弃
            # 结果；不能让返回的 md 又把“已中止”顶回待审核。
            batch = _batch_jobs.get(batch_id)
            if (not batch or g not in batch.get("groups", [])
                    or g.get("cancelled")
                    or int(g.get("attempt") or 0) != attempt):
                return
            g["md"] = md
            g["status"] = "done"
            # 池子重试会把整条转换重跑一遍，notes 里可能攒了同一句话两份，去重
            g["note"] = " ".join(dict.fromkeys(notes))
            batch = _batch_jobs.get(batch_id)
            if batch:
                _persist_batch(batch_id, batch)
        with _jobs_lock:
            if job_id in _jobs:
                _jobs[job_id].update(status="done", md=md)
                _persist_job(job_id, _jobs[job_id])
        # 不审核直接入库：转换成功即刻按默认值落库，不等人工点开这一组。**在批次锁
        # 之外**调用——入库要扫题库查重、写一堆 md 文件，占着锁会拖住同批其它并发组
        # 的状态更新（见 _convert_batch_worker 的并发说明）。
        if auto_import:
            _auto_import_after_convert(
                batch_id, g, attempt=attempt, md_snapshot=md)
    except Exception as e:
        with _batch_jobs_lock:
            batch = _batch_jobs.get(batch_id)
            if (not batch or g not in batch.get("groups", [])
                    or g.get("cancelled")
                    or int(g.get("attempt") or 0) != attempt):
                return
            g["error"] = str(e)
            g["status"] = "error"
            batch = _batch_jobs.get(batch_id)
            if batch:
                _persist_batch(batch_id, batch)
        with _jobs_lock:
            if job_id in _jobs:
                _jobs[job_id].update(status="error", error=str(e))
                _persist_job(job_id, _jobs[job_id])


def _set_collection_parent_error(batch_id: str, parent: dict,
                                 message: str, *, attempt: int,
                                 cache_dirs=()) -> None:
    """整本 OCR/结构分组失败时，保留父组和原 PDF 供重试。"""
    with _batch_jobs_lock:
        batch = _batch_jobs.get(batch_id)
        if (not batch or parent not in batch.get("groups", [])
                or int(parent.get("attempt") or 0) != attempt):
            return
        if parent.get("cancelled"):
            return
        parent["status"] = "error"
        parent["error"] = message
        if cache_dirs:
            cached = list(dict.fromkeys(str(path) for path in cache_dirs if path))
            parent["collection_cache_dirs"] = cached
            parent["cleanup_dirs"] = list(dict.fromkeys(
                list(parent.get("cleanup_dirs") or []) + cached))
        _persist_batch(batch_id, batch)
    with _jobs_lock:
        job = _jobs.get(parent["job_id"])
        if job is not None:
            job.update(status="error", error=message)
            _persist_job(parent["job_id"], job)


def _expand_collection_parent_inner(batch_id: str, parent: dict) -> None:
    """整本 OCR 一个无书签合集，再原子替换为 N 个普通子组。"""
    notes: list[str] = []
    with _batch_jobs_lock:
        batch = _batch_jobs.get(batch_id)
        if (not batch or parent not in batch.get("groups", [])
                or parent.get("cancelled")
                or parent.get("status") != "pending"):
            return
        attempt = int(parent.get("attempt") or 0)
        parent["status"] = "converting"
        parent["error"] = None
        max_units = _MAX_BATCH_GROUPS - (len(batch.get("groups", [])) - 1)
        _persist_batch(batch_id, batch)
    with _jobs_lock:
        job = _jobs.get(parent["job_id"])
        if job is not None:
            job.update(status="converting", error=None)
            _persist_job(parent["job_id"], job)

    try:
        units = _convert_with_ocr_credentials(
            _parse_ocr_backend(parent.get("ocr_backend", "")),
            lambda token, doc2x_key: converter.recognize_collection_units(
                parent["file_path"], parent.get("solution_path"),
                mineru_token=token, note_sink=notes.append,
                ocr_backend=_parse_ocr_backend(parent.get("ocr_backend", "")),
                doc2x_api_key=doc2x_key,
                cache_dirs=parent.get("collection_cache_dirs"),
                max_units=max_units))
    except Exception as exc:
        _set_collection_parent_error(
            batch_id, parent, str(exc), attempt=attempt,
            cache_dirs=getattr(exc, "workspace_dirs", ()))
        return

    if not units:
        _set_collection_parent_error(
            batch_id, parent, "整本识别完成，但没有找到可展开的分组",
            attempt=attempt)
        return

    cleanup_dirs = [unit.get("workspace_dir") for unit in units
                    if unit.get("workspace_dir")]
    with _batch_jobs_lock:
        batch = _batch_jobs.get(batch_id)
        if (not batch or parent not in batch.get("groups", [])
                or parent.get("cancelled") or batch.get("cancelled")
                or int(parent.get("attempt") or 0) != attempt):
            for directory in cleanup_dirs:
                converter.cleanup_collection_workspace(directory)
            return
        projected = len(batch["groups"]) - 1 + len(units)
        if projected > _MAX_BATCH_GROUPS:
            for directory in cleanup_dirs:
                converter.cleanup_collection_workspace(directory)
            message = (f"合集识别出 {len(units)} 组，本批展开后共 "
                       f"{projected} 组，超过上限 {_MAX_BATCH_GROUPS} 组")
            parent["status"] = "error"
            parent["error"] = message
            _persist_batch(batch_id, batch)
            with _jobs_lock:
                job = _jobs.get(parent["job_id"])
                if job is not None:
                    job.update(status="error", error=message)
                    _persist_job(parent["job_id"], job)
            return

        parent_index = batch["groups"].index(parent)
        used_gids = {g["gid"] for g in batch["groups"]}
        next_gid = max(used_gids, default=-1) + 1
        common_note = " ".join(dict.fromkeys(notes))
        children = []
        new_jobs = []
        for index, unit in enumerate(units):
            gid = parent["gid"] if index == 0 else next_gid
            if index:
                next_gid += 1
            job_id = uuid.uuid4().hex
            title = unit.get("title") or f"合集第 {index + 1} 组"
            filename = f"{title}.pdf"
            child = {
                "gid": gid, "job_id": job_id,
                # 左栏仍展示整本原 PDF；精确跳页需要上游坐标映射，
                # 但不影响 Markdown 分组与题解配对。
                "file_path": parent["file_path"],
                "solution_path": parent.get("solution_path"),
                "include_solution": (bool(unit.get("include_solution"))
                                     or parent.get("include_solution", False)),
                "only_numbers": parent.get("only_numbers"),
                "filename": filename,
                "engine": parent.get("engine") or _DEFAULT_ENGINE,
                "ocr_backend": _parse_ocr_backend(
                    parent.get("ocr_backend", "")),
                "block_mode": parent.get("block_mode") or _BLOCK_MODE_NO_AI,
                "num_template": parent.get("num_template") or "",
                # 原始两本合集只挂在第一个子组上，整批终态时统一删；
                # 其余子组不重复登记整本大文件。
                "cleanup_paths": (list(parent.get("cleanup_paths") or [])
                                  if index == 0 else []),
                # 先让首个子组承接整本缓存引用，再发布 batch；发布后虽会
                # 立即回收缓存，但崩溃在中间时任务快照仍能负责后续清理。
                "cleanup_dirs": ([unit["workspace_dir"]]
                                 + (list(parent.get("collection_cache_dirs") or [])
                                    if index == 0 else [])),
                "collection_mode": True,
                "collection_strategy": "ocr_structure",
                "collection_unit": True,
                "collection_raw_path": unit["raw_path"],
                "collection_ocr_meta": unit.get("ocr_meta") or {},
                # 结构合集的每个专题都引用同两本整集 PDF。明确把“原卷归属”
                # 固化在第一个子组上，避免 35 个子组自动入库时复制 35 遍；字段
                # 随 batch 快照持久化，进程重启或重试后仍由同一子组负责。
                "owns_collection_originals": index == 0,
                "collection_source_filename": parent.get("filename") or filename,
                "status": "pending", "md": None, "error": None,
                "pending": None, "note": common_note,
                "reviewed": None, "imported_count": 0,
                "attempt": 0,
                "in_flight": False,
            }
            children.append(child)
            new_jobs.append((job_id, {
                "status": "pending", "md": None, "error": None,
                "filename": filename, "path": parent["file_path"],
                "solution_path": parent.get("solution_path"),
                "ocr_backend": child["ocr_backend"],
            }))

        old_job_id = parent["job_id"]
        # 先发布批次这个用户可见真源，再写派生 job。若两份 JSON 之间
        # 恰好断电，restore_persisted_tasks 能按 batch 中的组补 job；反过来
        # 先写 job 会留下既无批次归属、也无法回收工作区的孤儿。
        batch["groups"][parent_index:parent_index + 1] = children
        _persist_batch(batch_id, batch)
    with _jobs_lock:
        for job_id, job_payload in new_jobs:
            _jobs[job_id] = job_payload
            _persist_job(job_id, job_payload)
        _jobs.pop(old_job_id, None)
    task_store.delete("job", old_job_id)
    for directory in parent.get("collection_cache_dirs") or []:
        converter.cleanup_collection_workspace(directory)


def _expand_collection_parent(batch_id: str, parent: dict) -> None:
    """串行保护父合集整本 OCR，并在外部调用前持久化缓存路径。"""
    with _batch_jobs_lock:
        batch = _batch_jobs.get(batch_id)
        if (not batch or parent not in batch.get("groups", [])
                or parent.get("cancelled")
                or parent.get("status") != "pending"
                or parent.get("in_flight")):
            return
        cache_dirs = list(parent.get("collection_cache_dirs") or [])
        if not cache_dirs:
            cache_dirs = converter.allocate_collection_cache_dirs(
                bool(parent.get("solution_path")))
            parent["collection_cache_dirs"] = cache_dirs
            parent["cleanup_dirs"] = list(dict.fromkeys(
                list(parent.get("cleanup_dirs") or []) + cache_dirs))
        parent["in_flight"] = True
        batch["running"] = batch.get("running", 0) + 1
        # 先落盘再发外部请求：进程若在 OCR 返回后崩溃，重启后的手动
        # 重转仍能找到缓存，也能由过期任务清理器回收。
        _persist_batch(batch_id, batch)
    try:
        _expand_collection_parent_inner(batch_id, parent)
    finally:
        with _batch_jobs_lock:
            batch = _batch_jobs.get(batch_id)
            if batch:
                batch["running"] = max(0, batch.get("running", 1) - 1)
                if parent in batch.get("groups", []):
                    parent["in_flight"] = False
                _persist_batch(batch_id, batch)


def _expand_pending_collections(batch_id: str) -> None:
    """在固定的子组线程池启动前，先展开所有无书签合集占位组。"""
    with _batch_jobs_lock:
        batch = _batch_jobs.get(batch_id)
        parents = [
            group for group in (batch or {}).get("groups", [])
            if (group.get("collection_strategy") == "ocr_structure"
                and not group.get("collection_unit")
                and group.get("status") == "pending")
        ]
    for parent in parents:
        _expand_collection_parent(batch_id, parent)


def _run_group_conversion(batch_id: str, group: dict) -> None:
    """给普通子组/单卷转换加真实在途锁，返回前禁止同目录重转。"""
    with _batch_jobs_lock:
        batch = _batch_jobs.get(batch_id)
        if (not batch or group not in batch.get("groups", [])
                or group.get("in_flight")):
            return
        group["in_flight"] = True
        batch["running"] = batch.get("running", 0) + 1
        _persist_batch(batch_id, batch)
    try:
        _convert_one_group(batch_id, group)
    finally:
        with _batch_jobs_lock:
            batch = _batch_jobs.get(batch_id)
            if batch:
                batch["running"] = max(0, batch.get("running", 1) - 1)
                if group in batch.get("groups", []):
                    group["in_flight"] = False
                _persist_batch(batch_id, batch)
        _maybe_finish_batch(batch_id)


def _convert_batch_worker(batch_id: str):
    """后台线程：把 _batch_jobs[batch_id] 的各组并发转换。

    并发度取 config.BATCH_CONVERT_CONCURRENCY（默认 3）。瓶颈是 MinerU 与 LLM
    的网络等待，不是本机 CPU，所以几组同时跑能把整批墙钟时间压下来；上限仍留着，
    免得一次几十组同时打上游接口。converter 的路径已在 ADR-024 里绝对化，几组
    并行不会因 os.chdir 互相串味。
    """
    with _batch_jobs_lock:
        batch = _batch_jobs.get(batch_id)
        if not batch:
            return
        batch["status"] = "converting"
        _persist_batch(batch_id, batch)

    # 必须在 pool.map 固定迭代列表之前展开；边跑边改 groups 会让
    # 新子组永远进不了本轮线程池，还可能与取消/持久化竞态。
    _expand_pending_collections(batch_id)
    with _batch_jobs_lock:
        batch = _batch_jobs.get(batch_id)
        if not batch:
            return
        groups = list(batch["groups"])

    def _run_one(g):
        # 起跑前再查一次取消标记：整批可能在排队等 worker 的时候就被中止了
        with _batch_jobs_lock:
            if (batch.get("cancelled") or g.get("cancelled")
                    or g.get("status") != "pending"):
                return
            is_collection_parent = (
                g.get("collection_strategy") == "ocr_structure"
                and not g.get("collection_unit"))
        # 另一条 batch worker 可能刚好在父组失败后、手动重转刚提交时仍在
        # 收尾。此时再次命中父占位组也只能走整本结构展开，不能退化成普通卷。
        if is_collection_parent:
            _expand_collection_parent(batch_id, g)
            return
        _run_group_conversion(batch_id, g)

    workers = max(1, int(getattr(config, "BATCH_CONVERT_CONCURRENCY", 1)))
    workers = min(workers, len(groups)) or 1
    with ThreadPoolExecutor(max_workers=workers,
                            thread_name_prefix=f"batch-{batch_id[:8]}") as pool:
        list(pool.map(_run_one, groups))

    with _batch_jobs_lock:
        # 展开/重转可能已替换组列表；终态必须以当前真源计算，不能用
        # pool 启动前的旧快照把批次永久写回 converting。
        groups = list(batch.get("groups") or [])
        if batch.get("cancelled"):
            for g in groups:
                if g["status"] in ("pending", "converting") and not g.get("md"):
                    g["cancelled"] = True
                    g["status"] = "error"
                    g["error"] = _CANCEL_UNSTARTED
                    g["reviewed"] = "skipped"
        # awaiting_block_review 也算「转换阶段结束」：它在等用户，不在等机器
        conv_done = all(g["status"] in ("done", "error", "awaiting_block_review")
                        for g in groups)
        batch["status"] = "done" if conv_done else "converting"
        _persist_batch(batch_id, batch)
    _maybe_finish_batch(batch_id)


def _group_terminal(g) -> bool:
    """只有明确导入或跳过才算最终态；普通失败还要保留原文件供重转。"""
    return (not g.get("refresh_in_progress")
            and g.get("reviewed") in ("imported", "skipped"))


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


def _group_cleanup_dirs(g) -> list[str]:
    """结构合集子组的独立 OCR 后工作区（去重）。"""
    return list(dict.fromkeys(
        str(path) for path in (g.get("cleanup_dirs") or []) if path))


def _maybe_finish_batch(batch_id: str):
    """若整批转换结束且各组都到最终态，清该批上传文件与 _jobs 条目（保留
    _batch_jobs 记录，使看板仍可显示汇总）。幂等：已清过则跳过。"""
    with _batch_jobs_lock:
        batch = _batch_jobs.get(batch_id)
        if not batch or batch.get("files_cleaned"):
            return
        if (batch.get("running", 0) > 0
                or any(g.get("in_flight") for g in batch.get("groups", []))):
            return
        conv_done = batch["status"] in ("done", "error")
        if not (conv_done and all(_group_terminal(g) for g in batch["groups"])):
            return
        batch["files_cleaned"] = True
        groups = list(batch["groups"])
        keep_files = bool(batch.get("target_folder_id"))
        _persist_batch(batch_id, batch)
    for g in groups:
        # 文件夹原卷面板发起的「重新转换」（target_folder_id 非空）：那两个文件是
        # **题库里的原卷本身**，不是临时上传件，删掉等于把用户的原卷弄没了。job
        # 快照仍要清（它只是内存条目）。与服务器版 import_convert.py 两处清理点
        # 的守卫逐条对应。
        if not keep_files:
            for p in _group_files(g):
                try:
                    Path(p).unlink()
                except OSError:
                    pass
        for directory in _group_cleanup_dirs(g):
            converter.cleanup_collection_workspace(directory)
        with _jobs_lock:
            _jobs.pop(g["job_id"], None)
        task_store.delete("job", g["job_id"])


def _clean_batch_uploads(batch_id: str):
    """取消整批时：删该批所有上传文件、释放 _batch_jobs 与 _jobs 条目。"""
    with _batch_jobs_lock:
        batch = _batch_jobs.pop(batch_id, None)
    if not batch:
        return
    keep_files = bool(batch.get("target_folder_id"))   # 理由同 _maybe_finish_batch
    for g in batch["groups"]:
        if not keep_files:
            for p in _group_files(g):
                try:
                    Path(p).unlink()
                except OSError:
                    pass
        for directory in _group_cleanup_dirs(g):
            converter.cleanup_collection_workspace(directory)
        with _jobs_lock:
            _jobs.pop(g["job_id"], None)
        task_store.delete("job", g["job_id"])
    task_store.delete("batch", batch_id)


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
    ocr_backend = _parse_ocr_backend(request.form.get("ocr_backend", ""))
    # 题号模板（仅逐块识别有效）。写法不合法就当场退回，别等后台线程才失败
    try:
        num_template = _parse_num_template(request.form.get("num_template", ""))
    except blocksplit.TemplateError as e:
        return jsonify(ok=False, error=f"题号模板写法不对：{e}"), 400
    # 设置页里启用的 LLM 配置，在请求线程里解析好再传给后台线程
    provider = providers.resolve_active()
    # 可选：单独的解析/答案文件（题干与解析分属两个文件时）
    sol_file = request.files.get("solution_file")
    try:
        _check_exam_file(file)
        if sol_file and sol_file.filename:
            _check_exam_file(sol_file)
    except _UploadRejected as exc:
        return jsonify(ok=False, error=str(exc)), 400

    config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    # 不在新任务开始时删除旧上传件：旧任务可能刚从快照恢复、仍等用户校对。
    # 未被任务引用的残件由 cleanup_output 启动清理，保留 24 小时。

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
        # solution_path 也登记进来：「一并保存原卷」要把答案卷一起存进文件夹，
        # 而它只能从这里查（表单传路径等于开一个任意文件读取接口）。
        # include_solution / only_numbers 也登记：校对页要照前者决定解析栏出不出、
        # 照后者算漏题（missing_numbers）。这两个只有上传那一刻知道，不存就丢了。
        # note 是切块阶段的告警（选项只剩标签之类），由 _convert_worker 写回。
        _jobs[job_id] = {"status": "pending", "md": None, "error": None,
                         "filename": orig_filename, "path": str(saved_path),
                         "solution_path": str(solution_path) if solution_path else None,
                         "include_solution": include_solution,
                         "only_numbers": only_numbers, "note": "",
                         "ocr_backend": ocr_backend}
        _persist_job(job_id, _jobs[job_id])
    threading.Thread(target=_convert_worker,
                     args=(job_id, saved_path, orig_filename, include_solution,
                           solution_path, only_numbers, provider, engine,
                           num_template, ocr_backend),
                     daemon=True).start()
    return jsonify(ok=True, job_id=job_id, filename=orig_filename)


@app.route("/convert/file/<job_id>")
def convert_file_view(job_id):
    """返回上传的原文件（供预览 iframe/img 显示）。

    直接用登记时存下的 path（服务端 uuid 命名、可信）。/convert/start 的单份路径
    存 UPLOAD_DIR，批量存 UPLOAD_DIR/batch/——都在 UPLOAD_DIR 之下。校验落在该
    目录内防穿越。
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


# --- 多组 PDF 批量导入 -----------------------------------------------------
#
# 页面上这一条现在叫「方式一：批量试卷转换」并排在最前（原先叫方式四，原方式一的
# 「单份 PDF/图片」入口已删除，与线上版一致）。本文件其余注释里的「方式四」都指
# 这条链路，「方式一」指那个已删除的单份入口——它的后端 /convert/start、
# /convert/status、/convert/file 仍保留：批量的每一组依旧登记为一个 job，校对页
# 的原卷对照和「重新转换」都在用同一套 _jobs 结构。

# 单批任务组数上限。**从 config 读**（原先是写死在这里的 20，与服务器版的 500
# 分叉了），现在两版都由各自 config 的 MAX_BATCH_GROUPS 决定，本地这份是 1000。
_MAX_BATCH_GROUPS = config.MAX_BATCH_GROUPS


class _UploadRejected(ValueError):
    """上传件不满足边界（组数/文件数/大小/格式）。仿服务器版的
    upload_guard.UploadValidationError，只是本地不需要 status_code 那一层。"""


def _file_size(storage) -> int:
    """FileStorage 的真实字节数。不信客户端给的 Content-Length：那是请求头里的
    数，装个改包工具就能写任意值，而这里要拿它跟单文件上限比。"""
    stream = storage.stream
    try:
        pos = stream.tell()
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
        stream.seek(pos)
        return size
    except (AttributeError, OSError):
        # werkzeug 的上传流正常都可 seek；走到这里说明是自定义客户端，直接拒。
        raise _UploadRejected("无法确认上传文件大小，请重新选择文件")


def _check_exam_file(storage):
    """单个试卷文件的格式、大小与真实内容；扩展名不能单独作为证据。"""
    name = storage.filename or ""
    ext = Path(name).suffix.lower()
    if ext not in config.EXAM_EXTS:
        # .doc 落在这里：pandoc 读不了旧版二进制格式（见 converter._docx_to_pdf），
        # 收下它只会让用户白等一趟转换。提前说清该怎么办。
        if ext == ".doc":
            raise _UploadRejected(
                f"「{name}」是旧版 .doc 格式，暂不支持，"
                f"请用 Word / WPS 另存为 .docx 后重新选择")
        raise _UploadRejected(
            f"「{name}」格式不支持，试卷只收 PDF、DOCX 与 PNG/JPG/WEBP/BMP 图片")
    is_image = ext in config.EXAM_IMAGE_EXTS
    limit = (config.MAX_EXAM_IMAGE_BYTES if is_image
             else config.MAX_EXAM_DOCUMENT_BYTES)
    if _file_size(storage) > limit:
        label = "图片" if is_image else "文档"
        raise _UploadRejected(
            f"「{name}」过大（{label}上限 {limit // (1024 * 1024)}MB）")
    stream = storage.stream
    pos = stream.tell()
    try:
        stream.seek(0)
        if is_image:
            try:
                with Image.open(stream) as image:
                    width, height = image.size
                    if width <= 0 or height <= 0 or width * height > 40_000_000:
                        raise _UploadRejected("图片像素过大（上限 4000 万像素）")
                    image.verify()
            except _UploadRejected:
                raise
            except Exception as exc:
                raise _UploadRejected(
                    f"「{name}」扩展名是图片，但内容无法解析") from exc
        elif ext == ".pdf":
            # 合法 PDF 的文件头允许在前 1KB 内（兼容 BOM 与少量前导字节）。
            if b"%PDF-" not in stream.read(1024):
                raise _UploadRejected(f"「{name}」扩展名是 PDF，但内容不是 PDF")
        else:
            try:
                stream.seek(0)
                with zipfile.ZipFile(stream) as archive:
                    infos = archive.infolist()
                    names = {item.filename for item in infos}
                    if ("[Content_Types].xml" not in names
                            or "word/document.xml" not in names):
                        raise _UploadRejected(
                            f"「{name}」扩展名是 DOCX，但内容不是 Word 文档")
                    if len(infos) > 2000 or sum(i.file_size for i in infos) > 250 * 1024 * 1024:
                        raise _UploadRejected("Word 文档解压后过大或内部文件过多")
            except _UploadRejected:
                raise
            except (OSError, zipfile.BadZipFile) as exc:
                raise _UploadRejected(
                    f"「{name}」扩展名是 DOCX，但内容不是 Word 文档") from exc
    finally:
        stream.seek(pos)


def _check_batch_files(idxs) -> None:
    """整批的文件数与逐个文件的格式/大小。

    **在落盘之前一次全查完**（照抄服务器版 batch_convert_create 的顺序）：第 10 组
    有坏文件时，前 9 组不该已经散落一地半成品文件在 uploads/batch/ 里等人清。
    """
    total = 0
    for i in sorted(idxs):
        for field, label in (("file", "题干"), ("solution_file", "解析")):
            files = [f for f in request.files.getlist(f"groups[{i}][{field}]")
                     if f and f.filename]
            if len(files) > config.MAX_FILES_PER_GROUP_SIDE:
                raise _UploadRejected(
                    f"第 {i + 1} 组的{label}文件过多"
                    f"（每组每侧上限 {config.MAX_FILES_PER_GROUP_SIDE} 个）")
            total += len(files)
            for f in files:
                _check_exam_file(f)
    if total > config.MAX_BATCH_FILES:
        raise _UploadRejected(
            f"本批文件过多（{total} 个，上限 {config.MAX_BATCH_FILES} 个）")


@app.route("/batch-convert/create", methods=["POST"])
def batch_convert_create():
    """接收多组文件配置，登记后台转换任务。

    带书签的合集仍可在这里零额外 OCR 拆卷；无书签合集只登记一个
    占位组，由后台将题干/解析各整本 OCR 一次，再按结构标题+连续题号
    动态展开为普通子任务。

    前端以 groups[i][file] / [solution_file] / [include_solution] / [only_numbers] /
    [collection_mode] 形式提交。返回 {ok, batch_id, count}。
    """
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
    try:
        _check_batch_files(idxs)
    except _UploadRejected as e:
        return jsonify(ok=False, error=str(e)), 400

    # 拆题选项整批统一（前端只在逐题识别时才发这两个字段）
    block_mode = _parse_block_mode(request.form.get("block_mode", ""))
    # OCR 服务默认整批统一；保留组内字段是为了程序化调用和将来的细粒度重试。
    batch_ocr_backend = _parse_ocr_backend(request.form.get("ocr_backend", ""))
    # 落点与免审开关，整批统一。层层依赖照抄服务器版：没勾上一层的，下一层直接
    # 按 False 收——不这么写，前端隐藏的子选项被人手动发上来就会生效。
    target_parent_id = request.form.get("target_parent_id", "").strip()
    if target_parent_id and not filestore.get_collection(target_parent_id):
        return jsonify(ok=False, error="目标父文件夹不存在，请刷新页面后重新选择"), 400
    pack_folder_name = ""
    if request.form.get("pack_folder") in ("1", "true", "on"):
        pack_folder_name = filestore.safe_folder_name(
            request.form.get("pack_folder_name", ""))
    auto_import = request.form.get("auto_import") in ("1", "true", "on")
    per_task_folder = auto_import and request.form.get("per_task_folder") in ("1", "true", "on")
    # 原卷要有个落点目录才存得下，所以它挂在 per_task_folder 这一层下面（照抄
    # 服务器版的层级）。pack_folder 那条路只有一个批文件夹，几十份原卷全堆进去
    # 认不出谁是谁，不如跟着「每任务一个文件夹」走。
    auto_keep_original = per_task_folder and request.form.get("auto_keep_original") in ("1", "true", "on")
    try:
        num_template = _parse_num_template(request.form.get("num_template", ""))
    except blocksplit.TemplateError as e:
        return jsonify(ok=False, error=f"题号模板写法不对：{e}"), 400

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

    def _resolve_input(paths, generated_paths):
        """把一组已落盘的文件解析成单个待转换文件路径：
        - 空 → None（无此文件）；
        - 单个 → 原样返回（PDF/Word/单图走各自原有分支）；
        - 多个 → 若全为图片，按序合成一个 PDF（复用 PDF 转换链路）；
          含非图片的多文件明确拒绝。PDF/Word 不能安全拼接，取第一个会让其余文件
          在任务显示成功后被清理，属于不可接受的静默丢失。

        合成失败（图片全都读不出来）抛 converter.ConvertError，**由调用方逐组接住**
        ——见下面循环里的注释。
        """
        if not paths:
            return None
        if len(paths) == 1:
            return paths[0]
        if all(converter.is_image_file(p) for p in paths):
            merged = config.BATCH_UPLOAD_DIR / f"{uuid.uuid4().hex}.pdf"
            generated_paths.append(str(merged))
            converter.images_to_pdf(paths, merged)
            return str(merged)
        raise converter.ConvertError(
            "同一组同一侧选择多个文件时只支持全部为图片；"
            "多个 PDF/Word 或图片与文档混选，请拆成不同任务组")

    prepared = []
    failed = []   # 建组阶段就失败的组：[(组号, 原因)]，转完后在看板上显示为错误

    def _discard(paths):
        """只回收本路由刚写进批量暂存目录的明确文件。"""
        for raw in dict.fromkeys(str(path) for path in paths if path):
            try:
                Path(raw).unlink()
            except OSError:
                pass

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
        ocr_backend = _parse_ocr_backend(
            request.form.get(f"groups[{i}][ocr_backend]", "")
            or batch_ocr_backend)
        collection_mode = request.form.get(
            f"groups[{i}][collection_mode]") in ("1", "true", "on")

        # 有书签时优先按书签零额外 OCR 预拆。无书签时不在这里报错：
        # 后台会让题干/解析各整本 OCR 一次，在 raw Markdown 中先分组，
        # 再逐组交给 blocksplit。绝不把重复起号的整本直接丢给切题器。
        if collection_mode:
            valid = (len(file_list) == 1
                     and Path(file_list[0]).suffix.lower() == ".pdf"
                     and len(sol_list) <= 1
                     and (not sol_list or Path(sol_list[0]).suffix.lower() == ".pdf"))
            if not valid:
                _discard(list(file_list) + list(sol_list))
                reason = "合集模式要求题干恰好一份 PDF，解析至多一份 PDF"
                failed.append((i + 1, reason))
                logger.warning("第 %d 组建组失败，已跳过：%s", i + 1, reason)
                continue
            try:
                parts = pdf_collection.split_collection_pair(
                    file_list[0], sol_list[0] if sol_list else None,
                    config.BATCH_UPLOAD_DIR, max_parts=_MAX_BATCH_GROUPS)
            except pdf_collection.NoBookmarksError:
                # 结构合集的实际份数要等 OCR 后才知道。先登记一个
                # 可持久化占位组，应用重启时不会自动重放付费识别。
                prepared.append({
                    "file_path": file_list[0],
                    "solution_path": sol_list[0] if sol_list else None,
                    "include_solution": bool(sol_list) or include_solution,
                    "only_numbers": only_numbers,
                    "filename": (request.files.getlist(
                        f"groups[{i}][file]")[0].filename or "无书签合集.pdf"),
                    "engine": engine, "ocr_backend": ocr_backend,
                    "cleanup_paths": list(file_list) + list(sol_list),
                    "cleanup_dirs": [],
                    "collection_mode": True,
                    "collection_strategy": "ocr_structure",
                    "collection_unit": False,
                })
                continue
            except pdf_collection.CollectionSplitError as e:
                _discard(list(file_list) + list(sol_list))
                failed.append((i + 1, str(e)))
                logger.warning("第 %d 组合集拆分失败，已跳过：%s", i + 1, e)
                continue
            for part_index, part in enumerate(parts):
                # 原始两份大合集只登记一次；拆出的单卷各归自己的组。整批终态时统一
                # 清理，既不会提前删掉并发中的输入，也不会重复保存整本合集为“原卷”。
                cleanup = [str(part.exam_path)]
                if part.solution_path:
                    cleanup.append(str(part.solution_path))
                if part_index == 0:
                    cleanup.extend(file_list)
                    cleanup.extend(sol_list)
                prepared.append({
                    "file_path": str(part.exam_path),
                    "solution_path": (str(part.solution_path)
                                      if part.solution_path else None),
                    "include_solution": bool(part.solution_path) or include_solution,
                    "only_numbers": only_numbers,
                    "filename": f"{part.title}.pdf",
                    "engine": engine, "ocr_backend": ocr_backend,
                    "cleanup_paths": cleanup,
                    "cleanup_dirs": [],
                    "collection_mode": True,
                    "collection_strategy": "bookmarks",
                    "collection_unit": True,
                })
            continue

        # 多图合成 PDF；单文件原样。原始上传件也留着，整批结束时统一清理。
        #
        # **合成失败只能废掉这一组，不能让整批 500**：从文件夹导入以后，多图组是
        # 常态（一个子目录的十几张扫描图＝一份卷子），几百组里混进一张坏图/空文件
        # 的概率不低。原先这里让 ConvertError 直接冒到 Flask，用户看到的是一个 500，
        # 前面几百组已落盘的文件还全留在 uploads/batch/ 里没人清。
        generated_paths = []
        try:
            file_path = _resolve_input(file_list, generated_paths)
            solution_path = _resolve_input(sol_list, generated_paths)
        except converter.ConvertError as e:
            _discard(list(file_list) + list(sol_list) + generated_paths)
            failed.append((i + 1, str(e)))
            logger.warning("第 %d 组建组失败，已跳过：%s", i + 1, e)
            continue
        # 选了解析文件即视为带解析（与 /convert/start 单份路径一致）
        if solution_path is not None:
            include_solution = True
        first = request.files.getlist(f"groups[{i}][file]")[0]
        # 显示名：单文件用原名；多文件合成时标「原名 等 N 张」
        first_name = first.filename
        n_files = len(file_list)
        disp_name = first_name if n_files == 1 else f"{first_name} 等 {n_files} 张"
        # 所有落盘文件（原始上传 + 合成 PDF）都要在清理时删除，避免残留
        extra = list(file_list) + list(sol_list) + generated_paths
        if file_path and file_path not in extra:
            extra.append(file_path)
        if solution_path and solution_path not in extra:
            extra.append(solution_path)
        prepared.append({
            "file_path": file_path, "solution_path": solution_path,
            "include_solution": include_solution,
            "only_numbers": only_numbers, "filename": disp_name,
            "engine": engine, "ocr_backend": ocr_backend,
            "cleanup_paths": extra, "cleanup_dirs": [],
            "collection_mode": False,
            "collection_strategy": "",
            "collection_unit": False,
        })

    if not prepared:
        if failed:
            detail = "；".join(f"第 {n} 组：{why}" for n, why in failed[:5])
            return jsonify(ok=False, error=f"没有可转换的任务组（{detail}）"), 400
        return jsonify(ok=False, error="没有有效的题干文件"), 400

    # 一张任务卡可展开为几十份卷，不能只在上传卡数量上检查上限。全部准备完再登记
    # _jobs，超限时能一次回收本次暂存件，不留下看板查不到的孤儿任务。
    if len(prepared) > _MAX_BATCH_GROUPS:
        _discard(path for item in prepared for path in item["cleanup_paths"])
        return jsonify(
            ok=False,
            error=f"合集拆分后共有 {len(prepared)} 份试卷，超过上限 {_MAX_BATCH_GROUPS} 份",
        ), 400

    groups = []
    for gid, item in enumerate(prepared):
        job_id = uuid.uuid4().hex
        with _jobs_lock:
            _jobs[job_id] = {
                "status": "pending", "md": None, "error": None,
                "filename": item["filename"], "path": item["file_path"],
                "ocr_backend": item["ocr_backend"],
            }
            _persist_job(job_id, _jobs[job_id])
        groups.append({
            "gid": gid, "job_id": job_id,
            "file_path": item["file_path"],
            "solution_path": item["solution_path"],
            "include_solution": item["include_solution"],
            "only_numbers": item["only_numbers"],
            "filename": item["filename"],
            "engine": item["engine"], "ocr_backend": item["ocr_backend"],
            "block_mode": block_mode, "num_template": num_template,
            "cleanup_paths": item["cleanup_paths"],
            "cleanup_dirs": item.get("cleanup_dirs") or [],
            "collection_mode": item["collection_mode"],
            "collection_strategy": item.get("collection_strategy") or "",
            "collection_unit": bool(item.get("collection_unit")),
            "collection_raw_path": item.get("collection_raw_path"),
            "collection_ocr_meta": item.get("collection_ocr_meta") or {},
            "status": "pending", "md": None, "error": None,
            "pending": None, "note": "",
            "reviewed": None, "imported_count": 0,
            "attempt": 0,
            "in_flight": False,
        })

    batch_id = uuid.uuid4().hex
    with _batch_jobs_lock:
        _batch_jobs[batch_id] = {"status": "converting", "groups": groups,
                                 "current_idx": 0, "files_cleaned": False,
                                 # created_at 给「转换任务」总面板排序用（第 N 批）
                                 "created_at": time.time(),
                                 "running": 0, "cancelled": False,
                                 # 落点/免审开关：整批共用，各组转完后各自读
                                 "pack_folder_name": pack_folder_name,
                                 "target_parent_id": target_parent_id,
                                 "auto_import": auto_import,
                                 "per_task_folder": per_task_folder,
                                 "auto_keep_original": auto_keep_original}
        _persist_batch(batch_id, _batch_jobs[batch_id])
    threading.Thread(target=_convert_batch_worker, args=(batch_id,),
                     daemon=True).start()
    # 跳到看板页（前端 window.location）。skipped 是建组阶段就废掉的组（多图合成
    # 失败），它们进不了 _batch_jobs，看板上不会有对应的卡片，所以必须在这次响应里
    # 说清楚——否则用户排了 20 组、看板只出现 19 张卡，不知道少的那组去哪了。
    return jsonify(ok=True, batch_id=batch_id, count=len(groups),
                   skipped=[{"group": n, "error": why} for n, why in failed],
                   dashboard=url_for("batch_dashboard", batch_id=batch_id))


@app.route("/batch-convert/status/<batch_id>")
def batch_convert_status(batch_id):
    """看板轮询：整批状态 + 各组状态（含 gid/是否已审/入库数），供实时刷新。"""
    with _batch_jobs_lock:
        batch = _batch_jobs.get(batch_id)
        if not batch:
            return jsonify(status="error", error="任务不存在"), 404
        groups = [_group_view(g) for g in batch["groups"]]
        all_terminal = all(_group_terminal(g) for g in batch["groups"])
        return jsonify(status=batch["status"], total=len(batch["groups"]),
                       groups=groups, all_done=all_terminal,
                       busy=_batch_busy(batch),
                       cancelled=bool(batch.get("cancelled")))


@app.route("/batch-convert/<batch_id>/cancel", methods=["POST"])
def batch_convert_cancel(batch_id):
    """中止整批：未开始的不花额度，在途调用跑完但结果作废。"""
    with _batch_jobs_lock:
        batch = _batch_jobs.get(batch_id)
        if not batch:
            return jsonify(ok=True)
        batch["cancelled"] = True
        running = _batch_busy(batch)
        for g in batch["groups"]:
            if g.get("reviewed") is not None:
                continue
            g["cancelled"] = True
            spent = g.get("status") != "pending"
            g["status"] = "error"
            g["error"] = _CANCEL_INFLIGHT if spent else _CANCEL_UNSTARTED
            g["md"] = None
            g["reviewed"] = "skipped"
        _persist_batch(batch_id, batch)
    if not running:
        _maybe_finish_batch(batch_id)
    return jsonify(ok=True)


def _cancel_group(batch_id: str, batch: dict, g: dict) -> str:
    """中止一组；调用方已持批次锁。"""
    g["cancelled"] = True
    spent = g.get("status") != "pending"
    g["status"] = "error"
    g["error"] = _CANCEL_INFLIGHT if spent else _CANCEL_UNSTARTED
    g["md"] = None
    # 单组中止后仍要等旧调用真正返回，再允许用户重转；不能先标成
    # skipped 让整批收尾把原文件删掉。用户若真不要该组可另点“跳过”。
    g["reviewed"] = None
    _persist_batch(batch_id, batch)
    return g["error"]


@app.route("/batch/<batch_id>/group/<int:gid>/cancel", methods=["POST"])
def batch_group_cancel(batch_id, gid):
    """只中止一组，不影响同批其余任务。"""
    with _batch_jobs_lock:
        batch = _batch_jobs.get(batch_id)
        if not batch:
            return jsonify(ok=False, error="任务不存在"), 404
        g = next((x for x in batch["groups"] if x["gid"] == gid), None)
        if not g:
            return jsonify(ok=False, error="任务组不存在"), 404
        if g.get("reviewed") == "imported":
            return jsonify(ok=False, error="该组已导入，无法中止"), 400
        if g.get("cancelled") or g.get("reviewed") == "skipped":
            return jsonify(ok=True, note=g.get("error") or "已跳过")
        note = _cancel_group(batch_id, batch, g)
    _maybe_finish_batch(batch_id)
    return jsonify(ok=True, note=note)


def _batch_neighbors(batch_id: str) -> dict:
    """当前批次在全部历史批次中的位置，完成后翻页仍保持稳定。"""
    rows = _all_batches(True)
    ids = [row["batch_id"] for row in rows]
    if batch_id not in ids:
        return {"index": 0, "total": len(ids), "prev": None, "next": None}
    index = ids.index(batch_id)
    return {
        "index": index + 1,
        "total": len(ids),
        "prev": ids[index - 1] if index else None,
        "next": ids[index + 1] if index + 1 < len(ids) else None,
    }


@app.route("/batch/<batch_id>")
def batch_dashboard(batch_id):
    """方式四看板页：列出各组转换/审核状态。转好的组亮出「审核入库」，
    转换在后台继续，用户可任意挑已就绪的组处理。"""
    with _batch_jobs_lock:
        batch = _batch_jobs.get(batch_id)
        if not batch:
            flash("批量任务已过期或不存在", "err")
            return redirect(url_for("import_md"))
        groups = [_group_view(g) for g in batch["groups"]]
        converting = any(g["status"] in ("pending", "converting",
                                           "awaiting_block_review")
                         for g in batch["groups"])
        cancelled = bool(batch.get("cancelled"))
        busy = _batch_busy(batch)
    return render_template(
        "batch_dashboard.html", batch_id=batch_id, groups=groups,
        converting=converting, cancelled=cancelled, busy=busy,
        batch_nav=_batch_neighbors(batch_id),
        batch_concurrency=max(1, int(getattr(config, "BATCH_CONVERT_CONCURRENCY", 1))))


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
        only_numbers = g.get("only_numbers")
        # 「识别解析 / 丢弃解析」是上传时勾的，校对页要照它决定解析栏出不出
        include_solution = bool(g.get("include_solution"))
        split_note = g.get("note") or ""
        num_template = g.get("num_template") or ""
        # 批量创建时勾了「该批全部放入同一文件夹」的，把名字带到校对页预填
        pack_folder_name = batch.get("pack_folder_name") or ""

    preview, all_cols, missing_numbers = _build_import_preview(
        md, include_solution=include_solution, only_numbers=only_numbers)
    batch_tag = Path(filename).stem
    return render_template(
        "import.html", preview=preview, raw=md, batch_tag=batch_tag,
        all_collections=all_cols, job_id=job_id,
        batch_id=batch_id, batch_gid=gid, queue_filename=filename,
        pack_folder_name=pack_folder_name, batch_source=batch_tag,
        missing_numbers=missing_numbers, split_note=split_note,
        num_template=num_template, include_solution=include_solution)


@app.route("/batch/<batch_id>/group/<int:gid>/skip", methods=["POST"])
def batch_group_skip(batch_id, gid):
    """跳过某组（不入库），标记已审，回看板。"""
    with _batch_jobs_lock:
        batch = _batch_jobs.get(batch_id)
        if batch:
            g = next((x for x in batch["groups"] if x["gid"] == gid), None)
            if g and g.get("in_flight"):
                flash("该组的旧转换尚未返回，请稍候再跳过", "err")
                return redirect(url_for("batch_dashboard", batch_id=batch_id))
            if g and g.get("reviewed") is None:
                g["reviewed"] = "skipped"
                _persist_batch(batch_id, batch)
    _maybe_finish_batch(batch_id)
    flash("已跳过该组", "ok")
    return redirect(url_for("batch_dashboard", batch_id=batch_id))


@app.route("/batch/<batch_id>/group/<int:gid>/reconvert", methods=["POST"])
def batch_group_reconvert(batch_id, gid):
    """审核时发现识别不对，把原文件重新送去转换一次。原文件在整批审核结束
    前都留在磁盘上（见 _maybe_finish_batch），故可直接复用 g["file_path"]
    /["solution_path"]，不需要用户重新上传。可顺带改一下「只取题号」再转，
    比如只有某几题识别错了，不用整份重来。

    也可顺带指定题号模板（`num_template`）：自动判方言切歪时，用户在校对页看到了
    实际的题号写法，这里指定后重跑一次比反复猜更直接（见 blocksplit.compile_dialect）。
    模板留空**保持上次的值不变**，不是清空——重转表单只填「只取题号」时，不该把
    上一轮好不容易调对的模板悄悄丢掉。要清空得显式提交 `num_template_clear`。
    """
    only_numbers_raw = request.form.get("only_numbers", "")
    tpl_raw = request.form.get("num_template", "")
    tpl_clear = request.form.get("num_template_clear") in ("1", "true", "on")
    refresh_imported = request.form.get("refresh_imported") in (
        "1", "true", "on")
    try:
        num_template = _parse_num_template(tpl_raw)
    except blocksplit.TemplateError as e:
        flash(f"题号模板写法有误：{e}", "err")
        return redirect(url_for("batch_group_review", batch_id=batch_id, gid=gid))
    with _batch_jobs_lock:
        batch = _batch_jobs.get(batch_id)
        if not batch:
            flash("批量任务已过期或不存在", "err")
            return redirect(url_for("import_md"))
        g = next((x for x in batch["groups"] if x["gid"] == gid), None)
        if not g:
            abort(404)
        # 中止过的组是例外：它的 reviewed 是 "skipped"（用户明确不要这一轮结果），
        # 但原文件还在，重转是合理诉求。真正不能重转的是已导入的，以及整批走完
        # 之后——那时上传文件已经被 _maybe_finish_batch 删了，重转只会立刻报
        # 文件不存在，不如在这里说清楚。
        is_imported_refresh = (
            refresh_imported and g.get("reviewed") == "imported")
        if g.get("reviewed") == "imported" and not is_imported_refresh:
            flash("该组已完成审核，无法重新转换", "err")
            return redirect(url_for("batch_dashboard", batch_id=batch_id))
        if is_imported_refresh and not (
                batch.get("auto_import")
                and g.get("collection_strategy") == "ocr_structure"
                and g.get("collection_unit")):
            flash("只有免审入库的结构合集子组支持安全刷新", "err")
            return redirect(url_for("batch_dashboard", batch_id=batch_id))
        if batch.get("files_cleaned"):
            flash("该批次的上传文件已清理，请重新上传", "err")
            return redirect(url_for("batch_dashboard", batch_id=batch_id))
        if g.get("reviewed") == "skipped" and not g.get("cancelled"):
            flash("该组已跳过，无法重新转换", "err")
            return redirect(url_for("batch_dashboard", batch_id=batch_id))
        if g.get("in_flight") or g.get("status") in ("pending", "converting"):
            flash("该组正在转换，请勿重复提交", "err")
            return redirect(url_for("batch_dashboard", batch_id=batch_id))
        is_collection_parent = (
            g.get("collection_strategy") == "ocr_structure"
            and not g.get("collection_unit"))
        if only_numbers_raw.strip():
            g["only_numbers"] = _parse_number_spec(only_numbers_raw)
        if tpl_clear:
            g["num_template"] = ""
        elif num_template:
            g["num_template"] = num_template
        # 指定了模板但引擎还是整篇识别时，自动切到逐题识别——模板只在逐块路径
        # 有效（整篇路径的切题在 LLM 里做，没有代码层的题号正则可钉）。不切的话
        # 用户填了模板却毫无变化，看不出是引擎不对。
        if g.get("num_template") and g.get("engine") != converter.ENGINE_BLOCK:
            g["engine"] = converter.ENGINE_BLOCK
            flash("题号模板只在「逐题识别」下生效，已自动切换识别方式", "ok")
        if is_imported_refresh and not g.get("refresh_in_progress"):
            previous_preview, _, previous_missing = _build_import_preview(
                g.get("md") or "",
                include_solution=g.get("include_solution", True),
                only_numbers=g.get("only_numbers"),
                existing_fps=set(), all_cols=[])
            previous_chosen = [item for item in previous_preview
                               if not item["dup"]]
            if (previous_missing
                    or len(previous_chosen) != int(g.get("imported_count") or 0)):
                flash("旧入库结果无法完整重建，已拒绝刷新", "err")
                return redirect(url_for("batch_dashboard", batch_id=batch_id))
            source = Path(g.get("filename") or "").stem or ""
            g["refresh_previous_items"] = _auto_import_items(
                previous_chosen, source)
            g["refresh_in_progress"] = True
        # 每次重转都换代。旧调用即使在“中止→立刻重转”后才返回，也只能
        # 发现代次已过期并丢弃结果，不能覆盖新一轮或触发旧结果自动入库。
        g["attempt"] = int(g.get("attempt") or 0) + 1
        g["status"] = "pending" if is_collection_parent else "converting"
        g["md"] = None
        g["error"] = None
        g["note"] = ""
        g["pending"] = None
        # 中止过的组也能重转（「中止」是放弃这一轮结果，不是永久封掉这一组）。
        # 两个标志都得清：cancelled 留着的话 _convert_one_group 转完不写 md，
        # reviewed 留着的话 _maybe_finish_batch 会把原文件当垃圾清掉。
        g["cancelled"] = False
        if not is_imported_refresh:
            g["reviewed"] = None
        job_id = g["job_id"]
    with _jobs_lock:
        if job_id in _jobs:
            _jobs[job_id].update(
                status="pending" if is_collection_parent else "converting",
                md=None, error=None)
            _persist_job(job_id, _jobs[job_id])
    with _batch_jobs_lock:
        batch = _batch_jobs.get(batch_id)
        if batch:
            _persist_batch(batch_id, batch)
    target = (_convert_batch_worker if is_collection_parent
              else _run_group_conversion)
    args = ((batch_id,) if is_collection_parent else (batch_id, g))
    threading.Thread(target=target, args=args, daemon=True).start()
    flash("已重新提交转换，转好后回到看板继续审核", "ok")
    return redirect(url_for("batch_dashboard", batch_id=batch_id))


# ---------------------------------------------------------------------------
# 拆题人工审核（逐题识别 + block_mode=manual）
#
# 切块结果先摆给人看：题号切歪、两题粘一起、解析被算进题干，这些在送 AI 之前
# 一眼能看出来，改完再送比事后逐题改便宜得多。
# ---------------------------------------------------------------------------


def _find_group(batch_id: str, gid: int):
    """取 (batch, group)。锁外用返回值只读/改字段，别再重新查一遍。"""
    batch = _batch_jobs.get(batch_id)
    if not batch:
        return None, None
    g = next((x for x in batch["groups"] if x["gid"] == gid), None)
    return batch, g


@app.route("/batch/<batch_id>/group/<int:gid>/blockimg/<path:name>")
def batch_group_block_image(batch_id, gid, name):
    """审核页里的插图。图还在 MinerU 的 extract_dir/images/ 下（此时尚未拦截
    到 assets），所以单独开一个只读该目录的出口。

    name 来自 URL、完全由用户控制：safe_join 挡掉 `..` 之类的写法，再用
    resolve 后的 parents 复核一遍——safe_join 不跟符号链接，两道都要留。
    """
    with _batch_jobs_lock:
        _, g = _find_group(batch_id, gid)
        pending = (g or {}).get("pending")
        dirs = [d["dir"] for d in (pending or {}).get("extract_dirs", [])]
    for d in dirs:
        base = Path(d) / "images"
        joined = safe_join(str(base), name)
        if not joined:
            continue
        p = Path(joined)
        if not p.is_file():
            continue
        if base.resolve() not in p.resolve().parents:
            continue
        return send_file(str(p))
    abort(404)


@app.route("/batch/<batch_id>/group/<int:gid>/blocks")
def batch_group_blocks(batch_id, gid):
    """拆题审核页：左边原文件，右边逐块可改。"""
    with _batch_jobs_lock:
        batch, g = _find_group(batch_id, gid)
        if not batch:
            flash("批量任务已过期或不存在", "err")
            return redirect(url_for("import_md"))
        if not g:
            abort(404)
        if g["status"] != "awaiting_block_review" or not g.get("pending"):
            flash("该组不在拆题审核状态", "err")
            return redirect(url_for("batch_dashboard", batch_id=batch_id))
        blocks = g["pending"]["blocks"]
        job_id, filename, note = g["job_id"], g["filename"], g.get("note", "")
        src = g["file_path"]

    # 左栏怎么显示原文件：PDF 用 iframe，图片用 img，其余只给下载链接
    suffix = Path(src or "").suffix.lower()
    if suffix == ".pdf":
        src_kind = "pdf"
    elif suffix in (".png", ".jpg", ".jpeg", ".bmp", ".webp"):
        src_kind = "image"
    else:
        src_kind = "other"
    return render_template("block_review.html", batch_id=batch_id, gid=gid,
                           blocks=blocks, job_id=job_id, filename=filename,
                           note=note, src_kind=src_kind)


def _finish_block_inflight(batch_id: str, gid: int, attempt: int) -> None:
    """释放人工拆题收尾的在途标记，并尝试批次终态清理。"""
    with _batch_jobs_lock:
        batch, g = _find_group(batch_id, gid)
        if batch:
            batch["running"] = max(0, batch.get("running", 1) - 1)
            if g and int(g.get("attempt") or 0) == attempt:
                g["in_flight"] = False
            _persist_batch(batch_id, batch)
    _maybe_finish_batch(batch_id)


def _finish_block_review_worker(batch_id: str, gid: int, pending: dict,
                                include_solution: bool, provider,
                                attempt: int):
    """后台线程：审核确认后把改好的块送 AI 收尾（状态字段与
    _convert_one_group 的 done/error 分支保持一致）。"""
    # 拦截最终图片时仍可能发现 OCR Markdown 引用的文件不存在。该提示必须回写
    # 任务并参与免审门控，不能因为已经过了拆题审核就丢在局部变量里。
    finish_notes: list[str] = []
    try:
        try:
            md = converter.finish_block_review(
                pending, action="ai", include_solution=include_solution,
                provider=provider, note_sink=finish_notes.append)
        except Exception as e:
            with _batch_jobs_lock:
                batch, g = _find_group(batch_id, gid)
                if (g and not g.get("cancelled")
                        and int(g.get("attempt") or 0) == attempt):
                    g["error"] = str(e)
                    g["status"] = "error"
                    g["pending"] = None
                    job_id = g["job_id"]
                    with _jobs_lock:
                        if job_id in _jobs:
                            _jobs[job_id].update(status="error", error=str(e))
                            _persist_job(job_id, _jobs[job_id])
                    _persist_batch(batch_id, batch)
            return
        with _batch_jobs_lock:
            batch, g = _find_group(batch_id, gid)
            if (not g or g.get("cancelled")
                    or int(g.get("attempt") or 0) != attempt):
                return
            g["md"] = md
            g["status"] = "done"
            g["pending"] = None
            if finish_notes:
                prior = [g.get("note") or "", *finish_notes]
                g["note"] = " ".join(dict.fromkeys(n for n in prior if n))
            job_id = g["job_id"]
            with _jobs_lock:
                if job_id in _jobs:
                    _jobs[job_id].update(status="done", md=md)
                    _persist_job(job_id, _jobs[job_id])
            _persist_batch(batch_id, batch)
    finally:
        _finish_block_inflight(batch_id, gid, attempt)


@app.route("/batch/<batch_id>/group/<int:gid>/blocks/confirm", methods=["POST"])
def batch_group_blocks_confirm(batch_id, gid):
    """确认拆题结果。action=ai 送 AI 规范化（后台跑，回看板等）；
    action=skip 机械渲染（不花额度，当场跑完直接进校对页）。"""
    action = request.form.get("action", "")
    if action not in ("ai", "skip"):
        return "action 只能是 ai 或 skip", 400
    try:
        edited = json.loads(request.form.get("blocks_json") or "[]")
    except ValueError:
        return "blocks_json 不是合法 JSON", 400
    if not isinstance(edited, list) or not edited:
        return "没有可提交的块", 400

    with _batch_jobs_lock:
        batch, g = _find_group(batch_id, gid)
        if not batch:
            flash("批量任务已过期或不存在", "err")
            return redirect(url_for("import_md"))
        if not g:
            abort(404)
        if g["status"] != "awaiting_block_review" or not g.get("pending"):
            flash("该组不在拆题审核状态", "err")
            return redirect(url_for("batch_dashboard", batch_id=batch_id))
        pending = dict(g["pending"])
        pending["blocks"] = edited   # 用户改过的块覆盖切块原结果
        include_solution = g["include_solution"]
        attempt = int(g.get("attempt") or 0)
        if g.get("in_flight"):
            flash("该组仍有转换在运行，请稍候重试", "err")
            return redirect(url_for("batch_dashboard", batch_id=batch_id))
        g["in_flight"] = True
        batch["running"] = batch.get("running", 0) + 1
        if action == "ai":
            # 先占住状态再起线程，免得看板在这一瞬间还显示「待拆题审核」
            g["status"] = "converting"
            g["pending"] = pending
        _persist_batch(batch_id, batch)
    if action == "ai":
        try:
            provider = providers.resolve_active()
            threading.Thread(
                target=_finish_block_review_worker,
                args=(batch_id, gid, pending, include_solution, provider, attempt),
                daemon=True).start()
        except Exception as exc:
            with _batch_jobs_lock:
                batch, current = _find_group(batch_id, gid)
                if current and int(current.get("attempt") or 0) == attempt:
                    current["status"] = "error"
                    current["error"] = str(exc)
                    _persist_batch(batch_id, batch)
            _finish_block_inflight(batch_id, gid, attempt)
            flash(f"提交 AI 失败：{exc}", "err")
            return redirect(url_for("batch_dashboard", batch_id=batch_id))
        flash("已提交，正在送 AI 规范化", "ok")
        return redirect(url_for("batch_dashboard", batch_id=batch_id))

    # skip：机械渲染很快，同步跑完直接进校对页
    finish_notes: list[str] = []
    try:
        try:
            md = converter.finish_block_review(
                pending, action="skip", include_solution=include_solution,
                note_sink=finish_notes.append)
        except Exception as e:
            with _batch_jobs_lock:
                batch, current = _find_group(batch_id, gid)
                if (current and not current.get("cancelled")
                        and int(current.get("attempt") or 0) == attempt):
                    current["error"] = str(e)
                    current["status"] = "error"
                    current["pending"] = None
                    _persist_batch(batch_id, batch)
            flash(f"渲染失败：{e}", "err")
            return redirect(url_for("batch_dashboard", batch_id=batch_id))
        with _batch_jobs_lock:
            batch, current = _find_group(batch_id, gid)
            if (current and not current.get("cancelled")
                    and int(current.get("attempt") or 0) == attempt):
                current["md"] = md
                current["status"] = "done"
                current["pending"] = None
                if finish_notes:
                    prior = [current.get("note") or "", *finish_notes]
                    current["note"] = " ".join(dict.fromkeys(
                        note for note in prior if note))
                with _jobs_lock:
                    if current["job_id"] in _jobs:
                        _jobs[current["job_id"]].update(status="done", md=md)
                        _persist_job(current["job_id"], _jobs[current["job_id"]])
                _persist_batch(batch_id, batch)
        return redirect(url_for(
            "batch_group_review", batch_id=batch_id, gid=gid))
    finally:
        _finish_block_inflight(batch_id, gid, attempt)


# ---------------------------------------------------------------------------
# 转换任务总面板：跨批看进度
#
# 单批看板只看得见自己那一批。排了几批之后就得有个总入口，否则离开页面就找不
# 回来了（batch_id 只在内存里，没有落盘的任务列表）。
# ---------------------------------------------------------------------------


def _empty_result(g) -> bool:
    """转好了但一道题都没有（空 md）。这种组没什么可审的，不计入「待审核」。"""
    return g["status"] == "done" and not (g.get("md") or "").strip()


def _group_view(g) -> dict:
    """看板与轮询共用显示口径，包含单组中止能力。"""
    empty = _empty_result(g)
    cancellable = (not g.get("cancelled")
                   and g.get("reviewed") is None
                   and g.get("status") in ("pending", "converting",
                                            "awaiting_block_review"))
    return {
        "gid": g["gid"], "filename": g["filename"],
        "ocr_backend": _parse_ocr_backend(g.get("ocr_backend", "")),
        "status": "error" if empty else g["status"],
        "error": ("转换完成但没有产出任何题目，请重新转换或检查原文件"
                  if empty else g.get("error")),
        "reviewed": g.get("reviewed"),
        "cancelled": bool(g.get("cancelled")),
        "cancellable": cancellable,
        "requires_review": qualcheck.requires_manual_review(g.get("note") or ""),
        "imported_count": g.get("imported_count", 0),
    }


def _batch_busy(batch) -> bool:
    """整批是否还在动。删除要挡在这上面：正在跑的组还会往 group 里写字段。"""
    return (batch["status"] == "converting" or batch.get("running", 0) > 0
            or any(g["status"] == "converting" for g in batch["groups"]))


def _batch_overview(bid: str, batch) -> dict:
    """一批的汇总行（字段与 batches-overview.js 读的键一一对应）。"""
    groups = batch["groups"]
    def _n(pred):
        return sum(1 for g in groups if pred(g))
    return {
        "batch_id": bid,
        "total": len(groups),
        # done = 转换阶段已结束（含失败与等人工审拆题），进度条用它
        "done": _n(lambda g: g["status"] in ("done", "error",
                                             "awaiting_block_review")),
        "converting": _n(lambda g: g["status"] == "converting"),
        "pending": _n(lambda g: g["status"] == "pending"),
        "ready": _n(lambda g: g["status"] == "done" and not g.get("reviewed")
                    and not _empty_result(g)),
        "awaiting_block_review": _n(
            lambda g: g["status"] == "awaiting_block_review"),
        "reviewed": _n(lambda g: g.get("reviewed") in ("imported", "skipped")),
        "errors": _n(lambda g: g["status"] == "error"),
        "status": batch["status"],
        "cancelled": bool(batch.get("cancelled")),
        "created_at": batch.get("created_at", 0),
        "finished": all(_group_terminal(g) for g in groups),
        "busy": _batch_busy(batch),
    }


def _all_batches(show_all: bool) -> list[dict]:
    """全部批次的汇总，按创建时间排序（第 1 批在最上）。"""
    with _batch_jobs_lock:
        rows = [_batch_overview(bid, b) for bid, b in _batch_jobs.items()]
    rows.sort(key=lambda r: (r["created_at"], r["batch_id"]))
    if not show_all:
        rows = [r for r in rows if not r["finished"]]
    return rows


@app.route("/batches")
def batches_overview():
    """转换任务总面板。默认只列还没处理完的批次。"""
    show_all = request.args.get("all") in ("1", "true", "on")
    concurrency = max(1, int(getattr(config, "BATCH_CONVERT_CONCURRENCY", 1)))
    return render_template("batches_overview.html",
                           batches=_all_batches(show_all),
                           show_all=show_all, concurrency=concurrency)


@app.route("/batches/status")
def batches_status():
    """总面板轮询。"""
    show_all = request.args.get("all") in ("1", "true", "on")
    return jsonify(ok=True, batches=_all_batches(show_all))


@app.route("/batch/<batch_id>/delete", methods=["POST"])
def batch_delete(batch_id):
    """从总面板删掉一批（连带清上传文件）。已经不在了也算成功——用户点的是
    「让它消失」，两次点击结果一致才不会看到莫名的报错。"""
    with _batch_jobs_lock:
        batch = _batch_jobs.get(batch_id)
        if not batch:
            return jsonify(ok=True)
        if _batch_busy(batch):
            return jsonify(ok=False, error="这一批还在转换，请先「中止整批」，"
                                           "等正在识别的几组落地后再删除"), 400
    _clean_batch_uploads(batch_id)
    return jsonify(ok=True)


# ---------------------------------------------------------------------------
# 查重
# ---------------------------------------------------------------------------

_dedup_jobs: dict[str, dict] = {}
_dedup_jobs_lock = threading.Lock()


def _dedup_worker(job_id: str, threshold: float) -> None:
    """后台扫描全库；页面请求不能被万级 Markdown 遍历和近似比较阻塞。"""
    try:
        rows = filestore.list_questions()
        with _dedup_jobs_lock:
            job = _dedup_jobs.get(job_id)
            if not job:
                return
            job.update(status="scanning", total=len(rows), compared=0)
        def report_progress(done, _total):
            with _dedup_jobs_lock:
                job = _dedup_jobs.get(job_id)
                if job:
                    job["compared"] = done
        # 保留完整题目字段，结果卡可继续走 qbody 的结构化渲染。
        groups = dedup.find_duplicates(
            [dict(row) for row in rows], threshold=threshold,
            progress=report_progress,
        )
        with _dedup_jobs_lock:
            job = _dedup_jobs.get(job_id)
            if job:
                job.update(status="done", groups=groups, finished_at=time.time())
    except Exception as exc:
        logger.exception("题库查重失败")
        with _dedup_jobs_lock:
            job = _dedup_jobs.get(job_id)
            if job:
                job.update(status="error", error=str(exc), finished_at=time.time())


@app.route("/dedup")
def dedup_page():
    """立即返回查重工作台；实际全库扫描由后台任务完成。"""
    threshold = request.args.get("threshold", type=float) or 0.85
    threshold = min(max(threshold, 0.5), 1.0)   # 限定合理范围
    return render_template("dedup.html", threshold=threshold)


@app.route("/api/dedup/start", methods=["POST"])
def dedup_start():
    payload = request.get_json(silent=True) or {}
    try:
        threshold = float(payload.get("threshold", 0.85))
    except (TypeError, ValueError):
        threshold = 0.85
    threshold = min(max(threshold, 0.5), 1.0)
    with _dedup_jobs_lock:
        # 连续点击不重复制造两份 O(n²) 工作；完成任务只留最近八份。
        for existing_id, existing in _dedup_jobs.items():
            if existing["status"] in {"loading", "scanning"}:
                return jsonify(ok=True, job_id=existing_id, reused=True)
        completed = sorted(
            ((job.get("finished_at", 0), key) for key, job in _dedup_jobs.items()),
            reverse=True,
        )
        for _finished, old_id in completed[8:]:
            _dedup_jobs.pop(old_id, None)
        job_id = uuid.uuid4().hex[:12]
        _dedup_jobs[job_id] = {
            "status": "loading", "threshold": threshold, "total": None,
            "groups": None, "error": "", "created_at": time.time(),
        }
    threading.Thread(
        target=_dedup_worker, args=(job_id, threshold),
        name=f"dedup-{job_id}", daemon=True,
    ).start()
    return jsonify(ok=True, job_id=job_id)


@app.route("/api/dedup/<job_id>")
def dedup_status(job_id):
    with _dedup_jobs_lock:
        job = _dedup_jobs.get(job_id)
        if not job:
            return jsonify(ok=False, error="查重任务不存在或已过期"), 404
        snapshot = dict(job)
    result = {
        "ok": True, "status": snapshot["status"],
        "total": snapshot.get("total"), "error": snapshot.get("error", ""),
        "compared": snapshot.get("compared", 0),
    }
    if snapshot["status"] == "done":
        groups = snapshot.get("groups") or []
        offset = max(request.args.get("offset", type=int) or 0, 0)
        limit = min(max(request.args.get("limit", type=int) or 20, 1), 50)
        page = groups[offset:offset + limit]
        next_offset = offset + len(page)
        template = "_dedup_results.html" if offset == 0 else "_dedup_groups.html"
        result.update(
            groups=len(groups),
            html=render_template(
                template, groups=page, total_groups=len(groups), job_id=job_id,
                next_offset=next_offset, has_more=next_offset < len(groups),
            ),
            append=offset > 0, next_offset=next_offset,
            has_more=next_offset < len(groups),
        )
    return jsonify(result)


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


# 查重页同时承载共享图片库体检。图片审计要读全部已登记题库，继续放后台线程，避免
# 万级 Markdown 扫描占住页面请求；删除只接受本进程生成且已完成的扫描快照。
_asset_audit_jobs: dict[str, dict] = {}
_asset_audit_jobs_lock = threading.Lock()


def _asset_audit_worker(job_id: str) -> None:
    try:
        scan = filestore.scan_orphan_assets()
        with _asset_audit_jobs_lock:
            job = _asset_audit_jobs.get(job_id)
            if job:
                job.update(status="done", scan=scan, finished_at=time.time())
    except Exception as exc:
        logger.exception("共享图片库审计失败")
        with _asset_audit_jobs_lock:
            job = _asset_audit_jobs.get(job_id)
            if job:
                job.update(status="error", error=str(exc), finished_at=time.time())


@app.route("/api/assets/orphans/start", methods=["POST"])
def asset_orphan_scan_start():
    with _asset_audit_jobs_lock:
        for existing_id, existing in _asset_audit_jobs.items():
            if existing.get("status") == "scanning":
                return jsonify(ok=True, job_id=existing_id, reused=True)
        completed = sorted(
            ((job.get("finished_at", 0), key)
             for key, job in _asset_audit_jobs.items()
             if job.get("status") != "scanning"),
            reverse=True,
        )
        for _finished, old_id in completed[8:]:
            _asset_audit_jobs.pop(old_id, None)
        job_id = uuid.uuid4().hex[:12]
        _asset_audit_jobs[job_id] = {
            "status": "scanning", "error": "", "created_at": time.time(),
        }
    threading.Thread(target=_asset_audit_worker, args=(job_id,),
                     name=f"asset-audit-{job_id}", daemon=True).start()
    return jsonify(ok=True, job_id=job_id)


@app.route("/api/assets/orphans/<job_id>")
def asset_orphan_scan_status(job_id):
    with _asset_audit_jobs_lock:
        job = _asset_audit_jobs.get(job_id)
        if not job:
            return jsonify(ok=False, error="图片扫描任务不存在或已过期"), 404
        status = job.get("status")
        if status == "error":
            return jsonify(ok=True, status="error", error=job.get("error", ""))
        if status != "done":
            return jsonify(ok=True, status=status)
        scan = job.get("scan") or {}
    return jsonify(
        ok=True, status="done", asset_dir=scan.get("asset_dir", ""),
        bank_count=scan.get("bank_count", 0),
        markdown_files=scan.get("markdown_files", 0),
        asset_files=scan.get("asset_files", 0),
        referenced_files=scan.get("referenced_files", 0),
        missing_references=scan.get("missing_references", 0),
        orphan_count=scan.get("orphan_count", 0),
        orphan_bytes=scan.get("orphan_bytes", 0),
        recent_unreferenced=scan.get("recent_unreferenced", 0),
        ignored_files=scan.get("ignored_files", 0),
    )


@app.route("/api/assets/orphans/<job_id>/delete", methods=["POST"])
def asset_orphan_delete(job_id):
    payload = request.get_json(silent=True) or {}
    if payload.get("confirm") is not True:
        return jsonify(ok=False, error="必须明确确认永久删除"), 400
    with _asset_audit_jobs_lock:
        job = _asset_audit_jobs.get(job_id)
        if not job:
            return jsonify(ok=False, error="图片扫描任务不存在或已过期"), 404
        if job.get("status") != "done" or not isinstance(job.get("scan"), dict):
            return jsonify(ok=False, error="图片扫描尚未完成，请稍后重试"), 409
        scan = job["scan"]
        job["status"] = "deleting"
    try:
        result = filestore.delete_scanned_orphan_assets(scan)
    except Exception as exc:
        logger.exception("共享图片库清理失败")
        with _asset_audit_jobs_lock:
            job = _asset_audit_jobs.get(job_id)
            if job:
                job.update(status="error", error=str(exc), finished_at=time.time())
        return jsonify(ok=False, error=str(exc)), 409
    with _asset_audit_jobs_lock:
        job = _asset_audit_jobs.get(job_id)
        if job:
            job.update(status="deleted", delete_result=result,
                       finished_at=time.time())
    return jsonify(ok=True, **result)


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


@app.route("/recycle-bin/questions/restore_selected", methods=["POST"])
def recycle_restore_selected():
    """勾选批量还原：不在回收站里的 id 静默跳过（页面可能是旧的，别为此报错）。"""
    ids = []
    seen = set()
    for v in request.form.getlist("qid"):
        v = (v or "").strip()
        if v and v not in seen:
            seen.add(v)
            ids.append(v)
    restored = 0
    for qid in ids:
        try:
            filestore.restore_question(qid)
        except KeyError:
            continue
        restored += 1
    if restored:
        flash(f"已恢复 {restored} 道题", "ok")
    else:
        flash("没有勾选要恢复的题目", "err")
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
    """设置页：OCR 凭证 / 识别模型（LLM）/ 外观主题。"""
    # list_llm_providers 自己就带 active_md / active_redraw / is_active 三位，
    # 别在这里再算一遍——多一处判定就多一处能跟存储对不上的真相。
    enriched = providers.list_llm_providers()
    prefs = ui_prefs.load()
    license_enforced = license_manager.is_enforced()
    # 未导入许可证时 load() 会直接返回 missing，不会创建设备身份；设置页必须主动
    # 生成请求码，测试者才能把它发给发布者签发首份许可证。
    device_state = device_identity.get_or_create() if license_enforced else None
    expected_device_id = (
        device_state.device_id if device_state is not None and device_state.valid else None
    )
    return render_template(
        "settings.html", providers=enriched,
        default_max_tokens=llm_client.MAX_TOKENS_DEFAULT,
        # has_mineru_token 而不是 token 本身：明文绝不进模板上下文，
        # 页面只显示「（已设置）」标记
        has_mineru_token=mineru_store.has_token(),
        # list_tokens 只给 id/备注/添加时间，密文与明文都不进模板
        mineru_tokens=mineru_store.list_tokens(),
        has_doc2x_key=doc2x_store.has_key(),
        doc2x_keys=doc2x_store.list_keys(),
        license_state=license_manager.load(
            expected_device_id=expected_device_id, require_device=license_enforced
        ),
        license_enforced=license_enforced,
        device_state=device_state,
        theme_mode=prefs["theme_mode"], theme_color=prefs["theme_color"],
        wallpaper=prefs["wallpaper"], swatches=ui_prefs.SWATCHES,
        wallpaper_is_video=ui_prefs.is_video_wallpaper(prefs["wallpaper"]))


@app.route("/settings/license", methods=["POST"])
def settings_license():
    """导入签名许可证；验签失败时保留当前许可证不动。"""
    uploaded = request.files.get("license_file")
    if uploaded is None or not uploaded.filename:
        flash("请选择 .qflicense 许可证文件", "error")
        return redirect(url_for("settings_page"))
    if Path(uploaded.filename).suffix.lower() != ".qflicense":
        flash("许可证文件扩展名必须是 .qflicense", "error")
        return redirect(url_for("settings_page"))
    raw = uploaded.stream.read(license_manager.MAX_LICENSE_BYTES + 1)
    state = license_manager.install(raw)
    if state.valid:
        flash(f"许可证已导入：{state.licensee}", "ok")
    else:
        flash(f"{state.summary}：{state.detail}", "error")
    return redirect(url_for("settings_page"))


@app.route("/settings/mineru", methods=["POST"])
def settings_mineru():
    """**追加**一份 MinerU token（可存多份，转换时按忙闲轮转）。

    这里不再沿用「留空提交＝清除」：能存多份之后，那个语义会变成「留空提交把
    我攒的几份全清了」，代价太大而且不可撤销。清除改成每条自己的删除按钮。
    """
    token = request.form.get("mineru_token", "").strip()
    label = request.form.get("mineru_label", "").strip()[:40]
    if not token:
        flash("请填入要添加的 MinerU Token", "err")
        return redirect(url_for("settings_page"))
    try:
        mineru_store.add_token(token, label)
    except crypto_utils.CryptoError as e:
        flash(str(e), "error")
        return redirect(url_for("settings_page"))
    flash("MinerU Token 已添加", "ok")
    return redirect(url_for("settings_page"))


@app.route("/settings/mineru/delete", methods=["POST"])
def settings_mineru_delete():
    """删掉一份 MinerU token（按 id）。"""
    token_id = request.form.get("token_id", "").strip()
    if mineru_store.remove_token(token_id):
        flash("已删除该 Token", "ok")
    else:
        flash("Token 不存在（可能已被删除）", "err")
    return redirect(url_for("settings_page"))


@app.route("/settings/doc2x", methods=["POST"])
def settings_doc2x():
    """追加一份 Doc2X API Key；页面从不回显明文。"""
    key = request.form.get("doc2x_key", "").strip()
    label = request.form.get("doc2x_label", "").strip()[:40]
    if not key:
        flash("请填入 Doc2X API Key", "err")
        return redirect(url_for("settings_page"))
    try:
        doc2x_store.add_key(key, label)
    except crypto_utils.CryptoError as e:
        flash(str(e), "error")
        return redirect(url_for("settings_page"))
    flash("Doc2X API Key 已添加", "ok")
    return redirect(url_for("settings_page"))


@app.route("/settings/doc2x/delete", methods=["POST"])
def settings_doc2x_delete():
    key_id = request.form.get("key_id", "").strip()
    if doc2x_store.remove_key(key_id):
        flash("已删除该 Doc2X API Key", "ok")
    else:
        flash("Doc2X API Key 不存在（可能已被删除）", "err")
    return redirect(url_for("settings_page"))


@app.route("/settings/theme", methods=["POST"])
def settings_theme():
    """存外观偏好：深浅色 / 主题色 / 壁纸（上传或移除）。"""
    mode = request.form.get("theme_mode", "light")
    color = request.form.get("theme_color", "").strip()
    patch = {"theme_mode": mode if mode in ("light", "dark") else "light"}
    # 只认 #rrggbb：这个值会被直接拼进 <html style="--primary: ...">，
    # 不校验就是一个 CSS 注入点（`red;} ... {`）。
    if re.fullmatch(r"#[0-9a-fA-F]{6}", color):
        patch["theme_color"] = color.lower()

    prefs = ui_prefs.load()
    if request.form.get("remove_wallpaper") == "1":
        _delete_wallpaper_file(prefs["wallpaper"])
        patch["wallpaper"] = None
    else:
        up = request.files.get("wallpaper")
        if up and up.filename:
            saved = _save_wallpaper(up)
            if saved is None:
                flash("壁纸必须是图片（≤25MB）或视频（≤100MB）", "error")
            else:
                _delete_wallpaper_file(prefs["wallpaper"])
                patch["wallpaper"] = saved
    ui_prefs.update(**patch)
    flash("外观已更新", "ok")
    return redirect(url_for("settings_page"))


_WALLPAPER_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif"}
_WALLPAPER_VIDEO_EXTS = {".mp4", ".webm", ".mov", ".m4v", ".ogv"}


def _save_wallpaper(up) -> str | None:
    """保存上传的壁纸，返回落盘文件名；类型/大小不合规则返回 None。

    文件名由后端生成（`wallpaper_<时间戳><后缀>`），不用上传的原名——原名
    完全由用户控制，而这个值随后会进 `url_for` 和磁盘路径。
    """
    ext = Path(up.filename).suffix.lower()
    if ext in _WALLPAPER_IMAGE_EXTS:
        limit = config.WALLPAPER_MAX_IMAGE
    elif ext in _WALLPAPER_VIDEO_EXTS:
        limit = config.WALLPAPER_MAX_VIDEO
    else:
        return None
    up.stream.seek(0, os.SEEK_END)
    size = up.stream.tell()
    up.stream.seek(0)
    if size == 0 or size > limit:
        return None
    config.WALLPAPER_DIR.mkdir(parents=True, exist_ok=True)
    name = f"wallpaper_{datetime.now().strftime('%Y%m%d_%H%M%S')}{ext}"
    up.save(str(config.WALLPAPER_DIR / name))
    return name


def _delete_wallpaper_file(name: str | None):
    """删掉旧壁纸文件。换壁纸时不删就会在 data/wallpaper 下无限堆积。"""
    if not name:
        return
    p = config.WALLPAPER_DIR / Path(name).name
    try:
        p.unlink(missing_ok=True)
    except OSError as e:
        logger.warning("删除旧壁纸失败 %s: %s", p, e)


@app.route("/wallpaper/<path:filename>")
def wallpaper_serve(filename: str):
    """壁纸静态服务。filename 来自 URL，用 send_from_directory 做目录约束
    （它内部走 safe_join，挡住 ../）。"""
    return send_from_directory(config.WALLPAPER_DIR, filename)


@app.route("/settings/llm", methods=["POST"])
def settings_llm():
    """识别模型的增删改启停。llm_action ∈ add|edit|activate|deactivate|remove。

    启停带用途：llm_purpose ∈ md|redraw（md = 导入识别，redraw = 配图重绘，
    必须是能吃图片的多模态模型）。**purpose 来自表单，一律先对着
    `providers.PURPOSES` 白名单校验**，不认的直接拒——别把它往下透传。
    """
    action = request.form.get("llm_action", "")
    purpose = request.form.get("llm_purpose", "md").strip() or "md"
    if purpose not in providers.PURPOSES:
        flash("参数不正确", "error")
        return redirect(url_for("settings_page"))
    purpose_label = "配图重绘" if purpose == "redraw" else "导入识别"
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
        # 「添加后用于」勾了哪几条，就在那几条用途上尝试点亮（该用途已有生效配置
        # 时不动，见 providers.add_llm_provider）。两条都没勾按 md 处理，免得填完
        # 一套配置一条路径都不生效。
        wanted = [p for p in providers.PURPOSES
                  if request.form.get(f"llm_for_{p}") in ("1", "true", "on")]
        if not wanted:
            wanted = ["md"]
        pid = providers.add_llm_provider(
            name=name, base_url=llm_client.normalize_base_url(base_url),
            api_key_enc=enc, model=model, max_tokens=max_tokens,
            purposes=tuple(wanted), supports_vision=("redraw" in wanted))
        if request.form.get("llm_activate_now") in ("1", "true", "on"):
            for p in wanted:
                providers.set_active_llm_provider(pid, p)
        flash(f"已添加识别模型「{name}」", "ok")
    elif action == "edit":
        pid = request.form.get("llm_id", "").strip()
        name = request.form.get("llm_name", "").strip()
        base_url = request.form.get("llm_base_url", "").strip()
        api_key = request.form.get("llm_api_key", "").strip()
        model = request.form.get("llm_model", "").strip()
        if not (pid and name and base_url and model):
            flash("名称、Base URL、模型名都要填；API Key 留空表示不修改", "error")
            return redirect(url_for("settings_page"))
        enc = None
        if api_key:
            try:
                enc = crypto_utils.encrypt_token(api_key)
            except crypto_utils.CryptoError as e:
                flash(str(e), "error")
                return redirect(url_for("settings_page"))
        updated = providers.update_llm_provider(
            pid, name=name, base_url=llm_client.normalize_base_url(base_url),
            model=model, max_tokens=llm_client.clamp_max_tokens(
                request.form.get("llm_max_tokens")), api_key_enc=enc,
            supports_vision=request.form.get("llm_supports_vision")
            in ("1", "true", "on"))
        flash("配置已更新" if updated else "配置不存在", "ok" if updated else "error")
    elif action in ("activate", "deactivate", "remove"):
        pid = request.form.get("llm_id", "").strip()
        if action != "deactivate" and not pid:
            flash("参数不正确", "error")
            return redirect(url_for("settings_page"))
        if action == "activate":
            providers.set_active_llm_provider(pid, purpose)
            flash(f"已切换「{purpose_label}」使用的模型", "ok")
        elif action == "deactivate":
            providers.deactivate_llm_providers(purpose)
            if purpose == "redraw":
                flash("已停用，配图重绘将回落到「导入识别」那套配置", "ok")
            else:
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


def _build_import_preview(raw: str, *, include_solution: bool = True,
                          only_numbers=None, existing_fps=None,
                          all_cols=None):
    """把规范化 md 文本切成预览题卡列表，附查重标记。

    返回 `(preview, all_cols, missing_numbers)`。单文件预览与批量 md 队列共用，
    保证两条路的切分/查重规则一致。

    **解析归位分两种形态**（症状是解析全混在题干里）：
      · 文档里有**两套题号**（题目 1..N 之后答案又从 1 数一遍，docx/PDF 卷子的
        常见排版）→ `importer.pair_duplicate_numbering` 按题号把后一套配成前一套
        的解析。这一档与 include_solution 无关地先做：要丢解析也得先知道哪半是
        解析，否则那一半会当成题留在库里。
      · 只有一套题号 → 在每块内部按 `答案/解析` 字样切
        （`split_solution(scan_markers=True)`）。
    `include_solution=False`（用户选了「丢弃解析」）时两档都照切，只是切出来的
    解析扔掉、不入库——比「假装没看见」稳，也免得答案文字残留在题干里。
    """
    # 两套题号先试：`split_questions` 取最长递增序列，第二套题号编号更小会被当成
    # 小问丢掉（那是它对的行为），所以两套题号必须由 split_questions_with_restart
    # 单独切一次。配好对之后 blocks 只剩前一套，解析由 paired 提供。
    restart = importer.split_questions_with_restart(raw)
    paired = None
    if restart is not None:
        rblocks, cut = restart
        paired = importer.pair_duplicate_numbering(
            [mechfix.fix_subq_parens(b) for b in rblocks], cut)
    if paired is not None:
        blocks = [b for b, _ in paired]
        pre_solutions = [s for _, s in paired]
    else:
        blocks = [mechfix.fix_subq_parens(b)
                  for b in importer.split_questions(raw)]
        pre_solutions = [None] * len(blocks)
    # 导入时不再自动扫描整座题库查重。文件式题库达到上万道后，这一步需要递归
    # 读取全部 Markdown，单次就可能耗时近一分钟；批量免审还会让用户误以为 OCR
    # 一直没有结束。历史库查重由独立「查重」页按需执行，这里只保留本次文本内部
    # 的 O(n) 指纹去重。existing_fps 仍作为显式注入口保留，供离线工具按需复用。
    if existing_fps is None:
        existing_fps = set()
    if all_cols is None:
        # 校对页下拉框只需要真实目录名，不需要每个文件夹的题数。带计数的默认树会
        # 为 641 个目录解析全库 Markdown，13k 题下仅打开校对页就要几十秒。
        all_cols = filestore.all_collections(filestore.list_collections_tree([]))
    preview = []
    seen_fps = set()
    found_numbers = set()
    for i, b in enumerate(blocks):
        # 先读块首题型标签定类型（逐块识别路径会打 `[单选]` 这类标签），再把标签
        # 剥掉——之后的切解析/查重/入库正文都用干净文本，标签不能进库
        qtype = importer.guess_type(b)
        b = importer.strip_type_tag(b)
        # 块内切解析：两套题号那档已经把解析摘出去了，块内不再扫标记——那时块里
        # 剩的 `答案：` 只可能是题干自带的字样（`根据答案：`），扫了是误伤。
        stem, solution = importer.split_solution(b, scan_markers=paired is None)
        if pre_solutions[i]:
            solution = pre_solutions[i]
        # 「参考答案」大标题落在上一题块尾（它前面没有新题号），剥掉免得拖进题干
        stem = importer.strip_answer_head(stem)
        n = importer.block_number(b)
        if n is not None:
            found_numbers.add(n)
        # 漏题检测要在剥题号之前做（block_number 靠题号定位），展示/入库用的
        # stem 则剥掉题号和紧随的分值标注——题卡渲染不需要这些。
        stem = importer.strip_leading_number(stem)
        # 「18. 证明：命题……」里的“证明”是题目指令，不是答案分区。但块内解析
        # 切分只看标记时会把整句话搬进 solution，留下空题干，自动入库因此失败。
        # 仅在解答题题干已经完全为空时回收整段；只要原题干还有一个字就不猜，避免
        # 把真正的解析覆盖回题干。
        if qtype == "解答题" and not stem.strip() and solution:
            stem = importer.strip_leading_number(solution)
            solution = None
        # whole/LLM 路径偶尔也会漏掉 MinerU 的 <sub>；机械路径虽已在 normalize_block
        # 处理过，这里再做一次幂等收口，确保任何识别入口都不会把 OCR HTML 入库。
        stem = mechfix.normalize_html_subscripts(stem)
        stem = mechfix.normalize_html_superscripts(stem)
        stem = mechfix.normalize_intrusive_column_text(stem)
        stem = mechfix.normalize_misplaced_constraints(stem)
        if solution:
            solution = mechfix.normalize_html_subscripts(solution)
            solution = mechfix.normalize_html_superscripts(solution)
            solution = mechfix.normalize_intrusive_column_text(solution)
            solution = mechfix.normalize_misplaced_constraints(solution)
        if qtype == "解答题":
            stem = mechfix.normalize_subquestion_layout(stem)
        stem = mechfix.ensure_fill_blank(stem, qtype)
        fp = dedup.fingerprint(stem)   # 指纹用题干，不含解析
        if fp in existing_fps:
            dup = "库中已存在"
        elif fp in seen_fps:
            dup = "本批重复"
        else:
            dup = None
        seen_fps.add(fp)
        # 裸字母/数字包 `$\displaystyle $` 是最后一步：查重指纹已经算完（dedup.normalize
        # 会把 `$` 和 `\displaystyle` 都删掉，包与不包指纹相同，顺序其实无关，但
        # 摆在指纹之后更明确——展示层的事不该有机会影响查重）。
        # 字母在前、数字在后：数字那步产出的 `$` 会被字母那步的零距离判据当成碎片
        # 信号，反序会挡掉一批本该包的字母（见 wrap_bare_numbers 的 docstring）。
        stem = mechfix.wrap_bare_numbers(mechfix.wrap_bare_letters(stem))
        if qtype in ("单选题", "多选题"):
            # 先利用 ``($A$)`` 这类带括号的强标签定位四个选项，再统一为 A.。
            # 旧顺序先剥括号，会把标签降成与题干中的点 A、事件 B 完全同形的弱
            # 标签，选项正文再出现 A/B 时整组就无法可靠识别。
            stem = mechfix.normalize_choice_options(stem, known_choice=True)
        if solution:
            solution = mechfix.wrap_bare_numbers(mechfix.wrap_bare_letters(solution))
            solution = mechfix.normalize_solution_layout(solution)
        # number 一路带到入库：文件名按它取（filestore._question_filename），
        # 没有它就只能落回 uuid 名。此前这个数字只用来做漏题检测就丢了。
        image_count = len(_QIMG_RE.findall(stem))
        img_mode, img_layouts, img_flow = _import_image_defaults(qtype, stem)
        final_solution = (solution or "") if include_solution else ""
        sol_img_split, sol_img_layouts = _import_solution_image_defaults(
            final_solution)
        preview.append({"idx": i, "body": stem,
                        "solution": final_solution,
                        "type": qtype, "dup": dup, "number": n,
                        "img_split": img_mode, "img_layouts": img_layouts,
                        "img_flow": img_flow, "image_count": image_count,
                        "sol_img_split": sol_img_split,
                        "sol_img_layouts": sol_img_layouts})
    missing_numbers = None
    if only_numbers:
        miss = sorted(set(only_numbers) - found_numbers)
        if miss:
            missing_numbers = miss
    elif len(found_numbers) >= 2:
        # 全卷导入也必须报自然题号断档。此前只在用户手动勾“指定题号”时检查，
        # 2025 三份真卷分别漏 3/12/18 题却全部显示可入库，质量门禁形同虚设。
        miss = sorted(set(range(min(found_numbers), max(found_numbers) + 1))
                      - found_numbers)
        if miss:
            missing_numbers = miss
    return preview, all_cols, missing_numbers


def _import_image_defaults(qtype: str, body: str, requested_mode: str = "",
                           requested_flow: str = "") -> tuple[str | None, list[dict], str]:
    """返回新导入题的 ``(图片位置, 逐图布局, 多图方向)``。

    默认值只写进新导入题，不迁移既有题卡。选择题的四图 A-D 配对优先于普通多图
    规则；否则按科目、题型和图片数量选位置。``requested_*`` 来自人工校对页，合法值
    永远优先，用户手改不能被默认值覆盖。
    """
    image_count = len(_QIMG_RE.findall(body or ""))
    if not image_count:
        return None, [], "column"
    allowed = {
        "单选题": ("pair", "opts", "full", "between", "after"),
        "多选题": ("pair", "opts", "full", "between", "after"),
        "解答题": ("sub", "full", "between", "after"),
        "填空题": ("full", "between", "after"),
    }.get(qtype, ())
    pair_default = (qtype in ("单选题", "多选题")
                    and qrender.pair_applies(body, qtype))
    if requested_mode in allowed:
        mode = requested_mode
    elif pair_default:
        mode = "pair"
    elif qtype in ("单选题", "多选题"):
        mode = "between" if image_count > 1 else "opts"
    elif qtype == "填空题":
        mode = "between" if config.BANK_SUBJECT == "physics" else "full"
    elif qtype == "解答题":
        mode = "after" if config.BANK_SUBJECT == "physics" else "sub"
    else:
        mode = None

    row_default = (
        (qtype in ("单选题", "多选题") and image_count > 1 and not pair_default)
        or (config.BANK_SUBJECT == "physics" and qtype in ("填空题", "解答题"))
    )
    flow = (requested_flow if requested_flow in ("row", "column")
            else "row" if row_default else "column")

    # 一图一选项由专用网格控制，普通图片组的 stack/align 在这条路径上不应介入。
    if mode == "pair":
        return mode, [], flow
    lead: dict = {"i": 0}
    if config.BANK_SUBJECT == "physics" and qtype == "解答题":
        lead["align"] = "center"
    if image_count > 1 and flow == "column":
        lead["stack"] = True
    layouts = [lead] if len(lead) > 1 else []
    return mode, layouts, flow


def _import_solution_image_defaults(solution: str) -> tuple[str | None, list[dict]]:
    """解析图片默认图文混排；多图作为一个纵向视觉组，避免横排挤压推导文字。"""
    image_count = len(_QIMG_RE.findall(solution or ""))
    if not image_count:
        return None, []
    layouts = [{"i": 0, "stack": True}] if image_count > 1 else []
    return "full", layouts


def _read_import_images(idx: int) -> list[tuple[bytes, str]]:
    """读取并校验校对卡新附加的图片，返回（字节、扩展名）。"""
    files = [f for f in request.files.getlist(f"images_{idx}")
             if f and f.filename]
    if len(files) > 20:
        raise _UploadRejected("每道题一次最多附加 20 张图片")
    images = []
    for storage in files:
        ext = Path(storage.filename or "").suffix.lower()
        if ext not in config.EXAM_IMAGE_EXTS:
            raise _UploadRejected(
                f"「{storage.filename}」不是支持的 PNG/JPG/WEBP/BMP 图片")
        _check_exam_file(storage)
        storage.stream.seek(0)
        images.append((storage.stream.read(), ext.lstrip(".")))
    return images


def _save_import_images(images: list[tuple[bytes, str]]) -> list[str]:
    """把已校验图片落入题库资产目录，返回 Obsidian 引用。"""
    token = f"import_{uuid.uuid4().hex}"
    return [filestore.save_image(token, i + 1, data, ext)
            for i, (data, ext) in enumerate(images)]


def _solution_display_name(exam_name: str, solution_path: str) -> str:
    """答案卷的展示名：题干名去掉扩展名 + 「（答案）」+ **答案文件自己的扩展名**。

    扩展名必须取自答案文件而不是题干：这两个可以是不同格式（题干 PDF、答案是
    拍的照片）。而扩展名一丢，Obsidian 就认不出文件类型，点开是一片空白。
    """
    stem = Path(exam_name).stem or "原卷"
    suffix = Path(solution_path).suffix
    return f"{stem}（答案）{suffix}"


def _group_paper_sources(grp: dict) -> list[tuple[str, str, str]]:
    """某组的原卷文件：[(磁盘路径, 展示名, kind)]。没有则空列表。

    多图组的 file_path 可能是 `_resolve_input` 合成的那份 PDF，存它而不是十张
    散图：用户要的是「这份卷子」，而且它正是送去识别的那份，与题目内容严格对应。
    """
    # OCR 后结构分组的每个子任务都指向同两本整集，只允许展开时明确选出的
    # “原卷归属子组”返回它们。旧任务没有该字段时按 False 处理，宁可不存，也
    # 不能在重启恢复后把整集复制到每个专题目录。书签预拆分有独立 PDF，不走此门。
    if (grp.get("collection_strategy") == "ocr_structure"
            and grp.get("collection_unit")
            and not grp.get("owns_collection_originals")):
        return []
    out: list[tuple[str, str, str]] = []
    name = (grp.get("collection_source_filename")
            or grp.get("filename") or "原卷")
    if grp.get("file_path"):
        out.append((grp["file_path"], name, "exam"))
    if grp.get("solution_path"):
        out.append((grp["solution_path"], _solution_display_name(name, grp["solution_path"]),
                    "solution"))
    return out


def _source_paper_files(job_id: str, batch_id: str, batch_gid) -> list[tuple[str, str, str]]:
    """本次导入对应的原卷文件，返回 [(磁盘路径, 展示名, kind), ...]。

    路径一律**从自己的任务/批次里查出来，绝不接受表单传路径**——否则等于开了一个
    「把本机任意文件复制进题库」的接口。查不到（粘贴 md 进来的、任务已被清掉的）
    就返回空列表，调用方据此静默跳过。
    """
    if batch_id and batch_gid is not None:
        with _batch_jobs_lock:
            batch = _batch_jobs.get(batch_id)
            grp = next((x for x in batch["groups"] if x["gid"] == batch_gid),
                       None) if batch else None
            return _group_paper_sources(grp) if grp else []
    if job_id:
        with _jobs_lock:
            job = _jobs.get(job_id)
            if not job:
                return []
            name = job.get("filename") or "原卷"
            out = []
            if job.get("path"):
                out.append((job["path"], name, "exam"))
            if job.get("solution_path"):
                out.append((job["solution_path"],
                            _solution_display_name(name, job["solution_path"]),
                            "solution"))
            return out
    return []


def _store_papers(folder_id: str, sources: list[tuple[str, str, str]]) -> int:
    """把原卷复制进文件夹，返回成功保存的份数。

    单份失败不影响其余、更不影响导入本身：题目已经入库了，为一个附件把整批校对
    成果回滚是最差的选择。失败只是少一个附件，用户还能事后自己拖进去——落点是
    vault 里的真目录，手动补一份没有任何门槛。
    """
    saved = 0
    for src, name, kind in sources:
        try:
            filestore.store_paper(src, folder_id, name, kind)
        except (OSError, ValueError):
            logger.warning("原卷保存失败，已跳过：%s", name)
            continue
        saved += 1
    return saved


def _auto_import_folder(batch: dict, grp: dict) -> str:
    """自动入库的落点文件夹 folder_id（''=题库根）。

    两个开关独立生效：pack_folder_name 全批共用一个文件夹；per_task_folder 每组
    各自一个以文件名命名的文件夹（若同时勾了上一条就挂在批文件夹下）。都没勾
    返回 ''，题目留在题库根。
    """
    # target_folder_id 优先级最高：直接从文件夹原卷面板发起的「重新转换」，题目
    # 应该落回那个文件夹，不新建也不复用别的开关。与服务器版同名逻辑一致。
    target_id = batch.get("target_folder_id")
    if target_id:
        return target_id
    batch_col = batch.get("target_parent_id") or ""
    if batch_col and not filestore.get_collection(batch_col):
        raise ValueError("目标父文件夹已不存在")
    pack_name = batch.get("pack_folder_name")
    if pack_name:
        batch_col = filestore.get_or_create_collection(pack_name, batch_col)
    # 有明确“父目录 + 年份”的人工审核批次同样需要每卷独立目录。此前
    # per_task_folder 被绑在 auto_import 上，免审时结构正确，人工审核反而把一整年
    # 的题全摊进年份目录；历史试卷导入正是人工审核优先，不能两条路径分叉。
    reviewed_year_batch = (not batch.get("auto_import")
                           and batch.get("target_parent_id")
                           and batch.get("pack_folder_name"))
    if batch.get("per_task_folder") or reviewed_year_batch:
        task_name = (filestore.safe_folder_name(Path(grp.get("filename") or "").stem)
                     or f"任务{grp['gid'] + 1}")
        return filestore.get_or_create_collection(task_name, batch_col)
    return batch_col


def _auto_import_items(chosen: list[dict], source: str) -> list[dict]:
    """把预览项转成文件存储 payload，首次入库与安全刷新共用同一口径。"""
    return [{
        "body": item["body"],
        "solution": item["solution"] or "",
        "type": item["type"],
        "source": source,
        "number": item.get("number"),
        **({"img_split": item.get("img_split"),
            "img_layouts": item.get("img_layouts") or []}
           if item.get("img_split") else {}),
        **({"sol_img_split": item.get("sol_img_split"),
            "sol_img_layouts": item.get("sol_img_layouts") or []}
           if item.get("sol_img_split") else {}),
    } for item in chosen]


def _auto_import_after_convert(batch_id: str, grp: dict, *, attempt: int,
                               md_snapshot: str) -> None:
    """「不审核直接入库」：转换成功后立即按系统默认值落库，跳过人工校对页。

    默认值与校对页「什么都不改」完全一致：题型用 importer.guess_type（已经在
    _build_import_preview 里算好），难度/标星留空，不加标签；同一份识别结果内部
    重复的题不导，历史题库不在这里自动查重。落库过程出意外时把这一组标成错误
    交给人工重转，不能让它卡在不死不活的状态——也不能拖累同批其它组（调用方
    在锁外触发）。
    """
    md = md_snapshot or ""
    if not md.strip():
        return
    with _batch_jobs_lock:
        current = _batch_jobs.get(batch_id)
        if (not current or grp not in current.get("groups", [])
                or grp.get("cancelled")
                or int(grp.get("attempt") or 0) != attempt
                or grp.get("md") != md):
            return
    # “免审”只省略正常结果的人工作业，不得吞掉高置信的结构丢失。此时保留 md、
    # 原文件和红色提示，状态仍为 done，用户可从看板进入普通校对页决定如何处理。
    if qualcheck.requires_manual_review(grp.get("note") or ""):
        with _batch_jobs_lock:
            current = _batch_jobs.get(batch_id)
            if (not current or grp not in current.get("groups", [])
                    or grp.get("cancelled")
                    or int(grp.get("attempt") or 0) != attempt):
                return
            grp["auto_review_blocked"] = True
            _persist_batch(batch_id, current)
        return
    try:
        preview, _, missing_numbers = _build_import_preview(
            md, include_solution=grp.get("include_solution", True),
            only_numbers=grp.get("only_numbers"),
            # 免审路径既不需要历史题库指纹，也不渲染文件夹下拉；显式传空值让
            # 这条性能边界不依赖 _build_import_preview 将来的默认行为。
            existing_fps=set(), all_cols=[])

        def _block_auto_import(reason: str) -> None:
            """把可复现的结构异常持久化，并保留原文件供重转。"""
            review_note = qualcheck.mark_manual_review(reason)
            with _batch_jobs_lock:
                active = _batch_jobs.get(batch_id)
                if (not active or grp not in active.get("groups", [])
                        or grp.get("cancelled")
                        or int(grp.get("attempt") or 0) != attempt):
                    return
                prior = [grp.get("note") or "", review_note]
                grp["note"] = " ".join(dict.fromkeys(
                    note for note in prior if note))
                grp["auto_review_blocked"] = True
                _persist_batch(batch_id, active)

        if missing_numbers:
            # 免审路径过去丢弃了预览阶段算出的缺号结果，导致「识别出了部分题」也会
            # 被当成完整试卷自动入库。缺失内容无法可靠猜回，必须把原结果留给人工
            # 校对；提示写回批次文件，应用重启后仍能看到阻断原因。
            missing_text = "、".join(str(n) for n in missing_numbers)
            _block_auto_import(
                f"识别结果缺少题号 {missing_text}，已停止自动入库，请进入校对页检查")
            return
        chosen = [p for p in preview if not p["dup"]]
        numbered = [p.get("number") for p in chosen
                    if isinstance(p.get("number"), int)]
        duplicate_numbers = sorted({
            number for number in numbered if numbered.count(number) > 1
        })
        if duplicate_numbers:
            shown = "、".join(str(number) for number in duplicate_numbers)
            _block_auto_import(
                f"识别结果存在重复题号 {shown}，已停止自动入库，请重新转换后检查")
            return
        if (grp.get("collection_strategy") == "ocr_structure"
                and grp.get("collection_unit")):
            # 无书签合集的专题已由整本结构判定确认从第 1 题起号；子组机械转换若
            # 又出现无题号块或不是完整的 1..N，说明块边界发生了二次漂移。不能让
            # 它凭文件顺序落库，否则“35 组均成功”会掩盖某组多题/漏首题。
            unnumbered = len(chosen) - len(numbered)
            unique_numbers = sorted(set(numbered))
            complete = (bool(unique_numbers)
                        and unique_numbers == list(range(1, unique_numbers[-1] + 1)))
            if unnumbered or not complete:
                detail = (f"有 {unnumbered} 道取不到题号" if unnumbered
                          else "题号不是从 1 开始的完整连续序列")
                _block_auto_import(
                    f"合集子组{detail}，已停止自动入库，请重新转换后检查")
                return
        if grp.get("refresh_in_progress"):
            previous_items = grp.get("refresh_previous_items") or []
            if len(chosen) != len(previous_items):
                _block_auto_import(
                    "安全刷新前后题目数量不一致，旧题已保留且未覆盖")
                return
        # 真正写题期间持有批次锁：取消请求若先到，这里会看到 cancelled
        # 并停止；若写题先开始，取消会等到本组被原子标成 imported 后再处理，
        # 不会出现“界面说已中止，旧结果却随后偷偷入库”的状态。
        with _batch_jobs_lock:
            current = _batch_jobs.get(batch_id)
            if (not current or grp not in current.get("groups", [])
                    or grp.get("cancelled")
                    or int(grp.get("attempt") or 0) != attempt
                    or grp.get("md") != md):
                return
            imported_count = 0
            if chosen:
                source = Path(grp.get("filename") or "").stem or ""
                folder = _auto_import_folder(current, grp)
                import_items = _auto_import_items(chosen, source)
                # 必须整组调用批量接口。逐题 create_question 会为每道题重新递归遍历
                # 整座 vault 计算最大 order；1.3 万题下一卷 20 题会白扫 20 遍。
                scope = f"batch:{batch_id}:group:{grp['gid']}"
                if grp.get("refresh_in_progress"):
                    previous_items = grp.get("refresh_previous_items") or []
                    created = filestore.refresh_questions_batch(
                        import_items, previous_items, folder,
                        idempotency_scope=scope)
                else:
                    created = filestore.create_questions_batch(
                        import_items, folder,
                        # 不含 attempt：同一组因崩溃/任务快照写失败而重转时必须认回
                        # 已写入的那部分；新上传会得到新的 batch_id，仍可明确再导一份。
                        idempotency_scope=scope)
                imported_count = len(created)
                # 一并保存原卷：落点跟题目同一个目录。没有落点文件夹（两个开关都没勾）
                # 时不存——原卷摊在题库根会跟题目混在一起，而且再也认不出属于哪一批。
                owns_originals = (
                    grp.get("collection_strategy") != "ocr_structure"
                    or not grp.get("collection_unit")
                    or bool(grp.get("owns_collection_originals")))
                if (current.get("auto_keep_original") and folder
                        and owns_originals):
                    _store_papers(folder, _group_paper_sources(grp))
            grp["reviewed"] = "imported" if imported_count else "skipped"
            grp["imported_count"] = imported_count
            grp.pop("refresh_in_progress", None)
            grp.pop("refresh_previous_items", None)
            grp.pop("auto_review_blocked", None)
            _persist_batch(batch_id, current)
    except Exception as exc:
        with _batch_jobs_lock:
            current = _batch_jobs.get(batch_id)
            if (not current or grp not in current.get("groups", [])
                    or int(grp.get("attempt") or 0) != attempt):
                return
            grp["status"] = "error"
            if grp.get("refresh_in_progress"):
                grp["error"] = (
                    f"安全刷新失败，旧题已保留且未覆盖：{exc}")
            else:
                grp["error"] = "自动入库失败，请点「重新转换」重试"
            _persist_batch(batch_id, current)
        return
    _maybe_finish_batch(batch_id)


# 批量 md 队列：queue_id -> {files: [{name, text}], pos: 已处理到第几个（0-based）}
# 本地单用户、内存态即可，会话结束丢弃。逐个文件过校对页导入/跳过后推进 pos。
_md_queues: dict[str, dict] = {}
_md_queues_lock = threading.Lock()


@app.route("/import/batch", methods=["POST"])
def import_batch_start():
    """接收多个 md 文件，建队列，重定向到第一个文件的校对页。"""
    files = [f for f in request.files.getlist("md_files") if f and f.filename]
    # 份数与体量上限，与服务器版 upload_guard 的 MAX_MD_* 对齐。整个队列连文本
    # 一起留在内存里（_md_queues），所以这里不设限的话几百份 md 就是几百份全文常驻。
    if len(files) > config.MAX_MD_FILES:
        flash(f"md 文件过多（{len(files)} 个，上限 {config.MAX_MD_FILES} 个）", "err")
        return redirect(url_for("import_md"))
    items = []
    total = 0
    for f in files:
        if Path(f.filename).suffix.lower() not in config.MD_EXTS:
            continue
        data = f.read()
        if len(data) > config.MAX_MD_FILE_BYTES:
            flash(f"「{f.filename}」过大（单文件上限 "
                  f"{config.MAX_MD_FILE_BYTES // (1024 * 1024)}MB）", "err")
            return redirect(url_for("import_md"))
        total += len(data)
        if total > config.MAX_MD_BATCH_BYTES:
            flash(f"md 文件总量超过上限（"
                  f"{config.MAX_MD_BATCH_BYTES // (1024 * 1024)}MB）", "err")
            return redirect(url_for("import_md"))
        items.append({"name": f.filename, "text": data.decode("utf-8", errors="replace")})
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

    # 队列里的 md 文件是用户手上唯一的一份文本，没有「丢弃解析」这个选项可言，
    # 一律按识别解析算——扔掉他没有第二次机会。
    preview, all_cols, _ = _build_import_preview(cur["text"])
    # 文件名去扩展名作整批标签默认值（方式四 name 形如「第 N 组 · 原名.pdf」，
    # 取原名部分去扩展名更合适）
    raw_name = cur["name"].split(" · ", 1)[-1]
    batch_tag = Path(raw_name).stem
    return render_template(
        "import.html", preview=preview, raw=cur["text"],
        batch_tag=batch_tag, all_collections=all_cols,
        job_id=cur.get("job_id", ""),
        queue_id=queue_id, queue_pos=pos + 1, queue_total=total,
        queue_filename=cur["name"], batch_source=batch_tag,
        include_solution=True)


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
        # 这里只需要一个“目标父文件夹”入口，不能为了填满原生 <select> 在首屏
        # 同步递归整个题库。真实库已有数百个目录，Windows 冷遍历会让导航卡数秒。
        # 文件夹选择器改走 /collections/children，用户展开哪一级才读取哪一级。
        return render_template("import.html", preview=None, all_collections=[])

    raw = request.form.get("md", "")
    # 上传文件优先
    file = request.files.get("file")
    if file and file.filename:
        raw = file.read().decode("utf-8", errors="replace")

    action = request.form.get("action", "preview")

    if action == "preview":
        # 默认整批标签：上传路径带来的文件名（去扩展名）
        batch_tag = request.form.get("batch_tag", "").strip()
        # 题源单独留着：它是「这批题从哪来」，只有走文件的路径才有意义。粘贴 md
        # 进来时没有文件名，不该把用户填的标签冒充成题源。
        batch_source = batch_tag
        # 粘贴路径（方式三）没有 batch_tag，改由表单里的「整批标签」直接带过来，
        # 否则校对页那个框永远是空的、每批都得手填一遍。
        if not batch_tag:
            batch_tag = request.form.get("tags_prefill", "").strip()
        job_id = request.form.get("job_id", "").strip()  # 上传路径才有，用于左侧 PDF 对照
        # 单文件转换的切块说明与「识别解析 / 丢弃解析」都存在 _jobs[job_id]
        # （_convert_worker / convert_start 写的），只有在这里读出来才看得见。
        # 直接粘贴 md 进来的没有 job_id，那时没有「丢弃解析」这个选项可言，
        # 默认识别——用户手上只有这一份文本，把解析扔掉他没有第二次机会。
        split_note = ""
        include_solution = True
        only_numbers = None
        with _jobs_lock:
            job = _jobs.get(job_id) if job_id else None
            if job:
                split_note = job.get("note") or ""
                include_solution = bool(job.get("include_solution", True))
                only_numbers = job.get("only_numbers")
        preview, all_cols, missing_numbers = _build_import_preview(
            raw, include_solution=include_solution, only_numbers=only_numbers)
        return render_template("import.html", preview=preview, raw=raw,
                               batch_tag=batch_tag, all_collections=all_cols,
                               job_id=job_id, batch_source=batch_source,
                               missing_numbers=missing_numbers,
                               split_note=split_note,
                               include_solution=include_solution)

    # confirm：按 body_<idx> 读逐题（用户改后）内容入库，不重新 split raw。
    # keep=保留的 idx（升序）。整批置顶、保持顺序。
    keep = sorted(int(x) for x in request.form.getlist("keep") if x.isdigit())
    batch_tags = [t.strip() for t in request.form.get("tags", "").split(",") if t.strip()]
    # 题源：整批共用的来源记录（试卷名/文件名），写进题的 source 字段而不是标签
    batch_source = request.form.get("batch_source", "").strip()
    target_col = request.form.get("collection", "").strip()   # 目标题集（可空=仅入库）
    # 试卷模式：把本批题打包进一个以试卷命名的文件夹（优先于上面的下拉选择）
    create_exam_folder = request.form.get("create_exam_folder") in ("1", "true", "on")
    exam_folder_name = request.form.get("exam_folder_name", "").strip()
    # 一并保存原卷：原卷得摊在某个文件夹里才认得出属于哪份卷子，所以没有落点
    # 文件夹时它自动失效（前端也跟着「打包为文件夹」显隐）
    keep_original = request.form.get("keep_original") in ("1", "true", "on")
    # 原卷路径要靠 job_id 从 _jobs 里查（表单里那个隐藏域一路带过来的）
    job_id = request.form.get("job_id", "").strip()
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
                    _persist_batch(batch_id, b)
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
        if job_id:
            # 单文件任务到这里已经完成审核。先取出路径再删快照；只允许删除
            # uploads 下的暂存件，原卷重新转换走 batch_id 分支，不会误碰 vault。
            with _jobs_lock:
                finished_job = _jobs.pop(job_id, None)
            task_store.delete("job", job_id)
            upload_root = config.UPLOAD_DIR.resolve()
            for key in ("path", "solution_path"):
                raw = (finished_job or {}).get(key)
                if not raw:
                    continue
                path = Path(raw)
                try:
                    path.resolve().relative_to(upload_root)
                    path.unlink(missing_ok=True)
                except (OSError, ValueError):
                    pass
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
        # 原卷题号：由校对页的隐藏域 num_<idx> 带回来（见 _import_preview.html）。
        # 拆分出来的新卡没有这个域，取到 None，落回 uuid 命名。
        num_raw = request.form.get(f"num_{idx}", "").strip()
        number = int(num_raw) if num_raw.isdigit() else None
        if body:
            try:
                new_images = _read_import_images(idx)
            except _UploadRejected as exc:
                flash(str(exc), "err")
                return redirect(url_for("import_md"))
            chosen.append((idx, body, solution, diff, starred, number,
                           new_images))
    if not chosen:
        if batch_id or queue_id:
            flash("未勾选任何题目，已跳过", "ok")
            return _after_import(0)
        flash("没有勾选任何题目", "err")
        return redirect(url_for("import_md"))
    # 落点文件夹要在建题**之前**定下来：题目直接建在目标目录里，而不是先落题库根
    # 再逐个搬。两个理由——① 文件名按题号取，`第3题.md` 先落在题库根会跟别的卷子
    # 的第 3 题撞名、白拿一个 `_<qid>` 后缀，搬过去时那个后缀还留着；② `order` 是
    # 按「落点目录里现有的最大值 +1」算的（`_top_order`），先落根再搬会把根目录的
    # 计数带进文件夹，那正是现有库里 0/2/4/6/8 这种跳号的来路。
    # 试卷模式优先：建（或复用）以试卷名命名的文件夹。撞同名时复用而不是报错中断。
    final_col = target_col
    folder_note = ""
    batch_folder = ""
    if batch_id and batch_gid is not None:
        with _batch_jobs_lock:
            current_batch = _batch_jobs.get(batch_id)
            current_group = next(
                (x for x in current_batch["groups"] if x["gid"] == batch_gid),
                None) if current_batch else None
        if current_batch and current_group:
            batch_folder = _auto_import_folder(current_batch, current_group)
    if batch_folder:
        final_col = batch_folder
        folder_note = f"并打包到文件夹「{batch_folder}」"
    elif create_exam_folder and exam_folder_name:
        final_col = filestore.get_or_create_collection(exam_folder_name, "")
        folder_note = f"并打包到文件夹「{exam_folder_name}」" if final_col else ""
    elif target_col:
        folder_note = "并加入所选题集"
    pending_questions = []
    pending_starred = []
    saved_image_names: set[str] = set()
    for idx, body, solution, diff, starred, number, new_images in chosen:
        per = [t.strip() for t in request.form.get(f"tag_{idx}", "").split(",") if t.strip()]
        tags = list(dict.fromkeys(batch_tags + per))
        # 题型优先取校对页逐题下拉（用户可手改），非法/缺失才回退正文特征猜测；
        # body 已是校对页剥过标签的干净文本，这里再兜底 strip 一次防标签残留入库
        body = importer.strip_type_tag(body)
        qtype = request.form.get(f"type_{idx}", "").strip()
        if qtype not in config.QUESTION_TYPES:
            qtype = importer.guess_type(body)
        if new_images:
            refs = _save_import_images(new_images)
            body = body.rstrip() + "\n\n" + "\n\n".join(refs)
            saved_image_names.update(
                name for ref in refs for name in _QIMG_RE.findall(ref))
        img_mode, img_layouts, _img_flow = _import_image_defaults(
            qtype, body,
            (request.form.get(f"img_mode_{idx}", "").strip()
             if request.form.get(f"img_mode_touched_{idx}") == "1" else ""),
            (request.form.get(f"img_flow_{idx}", "").strip()
             if request.form.get(f"img_flow_touched_{idx}") == "1" else ""))
        sol_img_split, sol_img_layouts = _import_solution_image_defaults(solution)
        pending_questions.append({
            "body": body, "solution": solution, "type": qtype,
            "tags": tags, "difficulty": diff, "source": batch_source,
            "number": number, "img_split": img_mode,
            "img_layouts": img_layouts,
            "sol_img_split": sol_img_split,
            "sol_img_layouts": sol_img_layouts,
        })
        pending_starred.append(starred)
    try:
        new_ids = filestore.create_questions_batch(pending_questions, final_col)
    except Exception:
        # 资产先落盘是为了让 Markdown 一次原子写入；若建题失败，立即清掉本次尚未
        # 被任何题引用的图片，避免校对失败制造孤儿文件。
        if saved_image_names:
            filestore.purge_orphan_images(saved_image_names)
        raise
    for qid, starred in zip(new_ids, pending_starred):
        if starred:
            filestore.toggle_starred(qid)

    # 「一并保存原卷」：原卷是**复制**进文件夹的，不是移动——批量审核期间那份
    # 临时文件还要供「重新转换」和左侧原文对照用。
    paper_note = ""
    if keep_original and final_col:
        sources = _source_paper_files(job_id, batch_id, batch_gid)
        saved = _store_papers(final_col, sources)
        if saved:
            paper_note = f"，并保存了 {saved} 份原卷"
        elif sources:
            paper_note = "（原卷保存失败，可稍后自己拖进该文件夹）"
    flash(f"已导入 {len(chosen)} 道题{folder_note}{paper_note}", "ok")
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
             # 双栏刷题的解答题作答区会结合难度与一级小问数计算；漏传时
             # exporter 只能把所有未知难度都按 3 处理，题卡上调的难度就失效。
             "difficulty": r["difficulty"],
             "solution": r["solution"], "img_align": r["img_align"],
             "img_width": r["img_width"], "img_split": r["img_split"],
             # 多图逐图排版设置（见 exporter._parse_layouts）；
             # 老题为空列表时导出退回 img_width/img_align 的单图行为
             "img_layouts": r["img_layouts"],
             # 解析里的图片排版设置，序号与题干各自独立编号。
             # 漏传这一项时 exporter._img_fields 取到 None，解析里的图会退回
             # 默认宽度/对齐——页面上调好的解析配图排版在导出里看不见。
             "sol_img_split": r["sol_img_split"],
             "sol_img_layouts": r["sol_img_layouts"]}
            for r in rows]


def _read_export_params():
    """从 request.form 读取所有导出参数（export 与 preview 共用）。"""
    # 纸张底色是所有模式共用的导出参数。白色是安全默认值：旧页面没有这个字段、
    # 或有人手工提交非法值时，不能把任意字符串继续拼进 pandoc 变量。
    paper_tone = request.form.get("paper_tone", "white")
    if paper_tone not in ("white", "cream"):
        paper_tone = "white"
    return dict(
        scope=request.form.get("scope", "selected"),
        mode=request.form.get("mode", "list"),
        title=request.form.get("title", "").strip() or "试卷",
        keypoints=request.form.get("keypoints", ""),
        solution_mode=request.form.get("solution_mode", "none"),
        paper_tone=paper_tone,
        wimath_logo=request.form.get("wimath_logo", "") in ("1", "true", "on"),
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
            "subject": request.form.get("subject", config.BANK_SUBJECT_LABEL),
            "info_bar": request.form.get("info_bar", "") in ("1", "true", "on"),
            "secret_notice": request.form.get("secret_notice", ""),
            "exam_notes": request.form.get("exam_notes", ""),
            # 四个键必须与 exporter._paginate_exam_std 里 sp.get(pkey) 的
            # pkey 逐字对应（single/multi/blank/solve）。值是**每小题分值数字**，
            # 题数与总分由 _std_section_desc 按实际选入的题数自动算；填的不是
            # 整句描述。键名写错不会报错，只会让分值说明整句消失。
            "section_points": {
                "single": request.form.get("points_single", ""),
                "multi": request.form.get("points_multi", ""),
                "blank": request.form.get("points_blank", ""),
                "solve": request.form.get("points_solve", ""),
            },
        },
        bank_subject=config.BANK_SUBJECT,
    )


# 导出/预览产物的取件号 -> 磁盘路径。
#
# **为什么要这一层**：本应用被 Obsidian 插件嵌在 iframe 里跑（Electron 渲染进程），
# 那个环境下有两件浏览器里能用的事是不能用的——
#   ① `URL.createObjectURL(blob)` 得到的 `blob:` URL 赋给 iframe 的 src：Electron
#      对 blob 文档的加载有自己的一套限制，表现是预览弹层里永远空白；
#   ② `as_attachment=True` 触发的下载：iframe 里的表单提交拿不到宿主的下载器，
#      表现是点「导出」什么都不发生。
# 两条都改成「先拿一个普通的 http GET 地址，再由它去取文件」：`http://127.0.0.1:PORT/...`
# 是最普通的同源请求，iframe 里能直接当 src，插件侧也能用 requestUrl 抓下来自己落盘。
#
# 只存路径不存内容：PDF 动辄几 MB，产物本来就已经在 OUTPUT_DIR 里了（由
# cleanup_output.py 定期清理），再在内存里留一份没有意义。
_out_files: dict[str, dict] = {}
_out_files_lock = threading.Lock()
# 取件号上限：单人使用，攒到这个数就把最早的丢掉（丢的只是取件号，文件还在
# OUTPUT_DIR 里）。不设上限的话开一天题库点上千次预览，这个 dict 只会长不会消。
_MAX_OUT_FILES = 64


def _register_out_file(path, download_name: str = "") -> str:
    """把导出产物登记成一个取件号，返回它。"""
    token = uuid.uuid4().hex
    with _out_files_lock:
        _out_files[token] = {"path": str(path),
                             "name": download_name or Path(path).name}
        while len(_out_files) > _MAX_OUT_FILES:
            _out_files.pop(next(iter(_out_files)))
    return token


@app.route("/api/handouts/rendered-card/<digest>")
def handout_rendered_card(digest):
    """稳定读取单题 SVG 缓存；请求只能提供规范 SHA-256 摘要，不能提供路径。"""
    if not re.fullmatch(r"[0-9a-f]{64}", str(digest or "")):
        abort(404)
    cache_dir = config.OUTPUT_DIR / "handout_card_cache"
    if cache_dir.is_symlink():
        abort(404)
    try:
        root = cache_dir.resolve(strict=True)
        candidate = cache_dir / f"{digest}.svg"
        if candidate.is_symlink():
            abort(404)
        path = candidate.resolve(strict=True)
    except OSError:
        abort(404)
    if root != path.parent or not path.is_file():
        abort(404)
    response = send_file(path, as_attachment=False, mimetype="image/svg+xml",
                         conditional=True)
    response.headers["Content-Security-Policy"] = "sandbox"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.route("/outfile/<token>")
def out_file(token):
    """按取件号取导出产物。`?dl=1` 走下载，否则内联（供 iframe 预览）。

    路径**只从 `_out_files` 里查，不接受任何来自请求的路径片段**——否则这就是一个
    「读本机任意文件」的接口。取件号是 uuid，猜不到；查不到一律 404。
    """
    with _out_files_lock:
        item = _out_files.get(token)
    if not item:
        abort(404)
    path = Path(item["path"])
    if not path.is_file():
        abort(404)
    as_attachment = request.args.get("dl") in ("1", "true", "on")
    # 题卡局部编译同样使用取件号，但返回 SVG；不能再把所有内联产物硬标成 PDF。
    inline_types = {".pdf": "application/pdf", ".svg": "image/svg+xml"}
    kwargs = {} if as_attachment else {
        "mimetype": inline_types.get(path.suffix.lower(), "application/octet-stream")}
    response = send_file(path, as_attachment=as_attachment,
                         download_name=item["name"], **kwargs)
    if path.suffix.lower() == ".svg":
        response.headers["Content-Security-Policy"] = "sandbox"
        response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.route("/preview", methods=["POST"])
def preview():
    """生成真实 PDF，返回它的取件地址（JSON），供前端 iframe 嵌入预览。

    返回地址而不是直接返回 PDF 字节：前端拿到字节只能走 `createObjectURL`，
    而那条路在 Obsidian 的 Electron 环境里嵌不进 iframe（见 `_out_files` 的注释）。
    """
    p = _read_export_params()
    questions = _collect_questions(p["scope"])
    if not questions:
        return jsonify(ok=False, error="没有可预览的题目"), 400

    try:
        out_path = service_ports.export_document(
            questions, title=p["title"], fmt="pdf", mode=p["mode"],
            keypoints=p["keypoints"], fullpage_ids=p["fullpage_ids"],
            header_footer=p["header_footer"], solution_mode=p["solution_mode"],
            std_opts=p["std_opts"], paper_tone=p["paper_tone"],
            wimath_logo=p["wimath_logo"], bank_subject=p["bank_subject"])
    except exporter.ExportError as e:
        return jsonify(ok=False, error=f"预览生成失败：{e}"), 500

    token = _register_out_file(out_path)
    return jsonify(ok=True, url=url_for("out_file", token=token))


@app.route("/export", methods=["POST"])
def export():
    """生成导出产物，返回取件地址（JSON）。

    同样不直接回文件流：`as_attachment` 的下载在 iframe 里不会发生（宿主没有下载
    器），前端拿到地址后自己决定怎么落地——独立浏览器里点一个 `<a download>`，
    Obsidian 里则把地址交给插件，由插件抓下来写进 vault（见 base.html 的桥）。
    """
    p = _read_export_params()
    fmt = request.form.get("fmt", "pdf")             # pdf / tex / zip（仅导出有）

    questions = _collect_questions(p["scope"])
    if not questions:
        return jsonify(ok=False, error="没有可导出的题目"), 400

    try:
        out_path = service_ports.export_document(
            questions, title=p["title"], fmt=fmt, mode=p["mode"],
            keypoints=p["keypoints"], fullpage_ids=p["fullpage_ids"],
            header_footer=p["header_footer"], solution_mode=p["solution_mode"],
            std_opts=p["std_opts"], paper_tone=p["paper_tone"],
            wimath_logo=p["wimath_logo"], bank_subject=p["bank_subject"])
    except exporter.ExportError as e:
        return jsonify(ok=False, error=f"导出失败：{e}"), 500

    # 下载名用「试卷标题 + 产物扩展名」，而不是 exporter 内部那个带时间戳和 uuid 的
    # 工作目录文件名：存进 vault 之后要靠这个名字认出是哪份卷子。
    name = (filestore.safe_folder_name(p["title"]) or "试卷") + Path(out_path).suffix
    token = _register_out_file(out_path, name)
    return jsonify(ok=True, url=url_for("out_file", token=token, dl=1),
                   filename=name)


cleanup_output.run_cleanup()
restore_persisted_tasks()


if __name__ == "__main__":
    # 仅本地监听，无鉴权。
    # debug 默认关闭：开启时 Werkzeug reloader 会另派生一个子进程，父进程被
    # 杀掉后子进程仍占着端口（Obsidian 插件托管本进程时会残留），故只在显式
    # 设置 QUIZFORGE_DEBUG=1 时才开。
    port = int(os.environ.get("QUIZFORGE_PORT", "5000"))
    debug = os.environ.get("QUIZFORGE_DEBUG", "") == "1"
    app.run(host="127.0.0.1", port=port, debug=debug)
