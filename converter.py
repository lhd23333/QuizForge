"""转换接口层：上传的 PDF/图片 → project-alpha 的 MinerU+DeepSeek → 规范化 md。

与 quizbank（单机版）的唯一关键差异：MinerU token 不再从 project-alpha 的
.env 读，而是由调用方（app.py）传入当前登录用户自己的 token（BYOK，解决
多人共用一个 MinerU 账号导致的每日额度排队问题）。

规范化那一步的 LLM 也可换：调用方传 provider（llm_provider.ProviderConfig，
站点默认或用户自己的配置）时，用 QuizForge 自己的 llm_client.LLMClient
（任意 OpenAI 兼容服务，且 max_tokens 可配）；传 None 时回落 project-alpha 的
DeepSeekClient + 其 .env 里的集中 key，即本功能上线前的老行为。

做法：project-alpha 的 `load_config()` 返回一个 dataclass Config
(mineru_token, deepseek_api_key, ...)，这里读出来之后，用
`dataclasses.replace(cfg, mineru_token=user_mineru_token)` 换掉 mineru_token
字段再往下传——不改 project-alpha 一行代码。

其余逻辑（sys.path 注入、图片拦截、双文件解析关联）与 quizbank 的
converter.py 基本一致，但**中间产物路径的解析方式不同**：多用户版这边改成了
绝对路径 + 只在读配置时短暂切 CWD（见 _alpha_cwd / _raw_md_dir），因为方式四
的批量转换现在是并发跑多组的（config.BATCH_CONVERT_CONCURRENCY）。
"""

import dataclasses
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import logging
import uuid
import zlib
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from difflib import SequenceMatcher

import config
import corpus
import doc2x_client
import mineru_store
import ocr_pool
import imgorder
import llm_client
import optcheck
import qualcheck
import collection_structure
import collection_recovery

logger = logging.getLogger(__name__)

BOUNDARY_AUTO = "auto"
BOUNDARY_WHITELIST = "whitelist"


def normalize_boundary_mode(raw: str) -> str:
    """拆题边界策略。未知值保持旧版的智能识别行为。"""
    return (BOUNDARY_WHITELIST
            if str(raw or "").strip() == BOUNDARY_WHITELIST
            else BOUNDARY_AUTO)

# project-alpha 的中间产物根目录（MinerU 解压的 md 与图片都落在这里）。
# 以前这里用的是相对路径 "output/raw_md"，靠调用前 os.chdir 到 project-alpha
# 根来解析；现在改成绝对路径。原因：CWD 是**进程级**状态、不是线程级，方式四
# 并发转换多组时，A 组在 finally 里把 CWD 切回来的瞬间，B 组正拿相对路径落盘，
# 文件就会写到 quizbank-web 目录下（或直接 FileNotFoundError）。绝对路径没有
# 这个竞态，也让日志里的路径一眼能看出来在哪。
_RAW_MD_ROOT = config.OCR_WORKSPACE_ROOT


def _raw_md_dir(stem: str) -> Path:
    """某个输入文件对应的中间产物目录（绝对路径）。"""
    return _RAW_MD_ROOT / stem


def _dual_ocr_workspaces(exam_path: Path, solution_path: Path):
    """为题干/解析各分配一个不会互相覆盖的 OCR 工作区。

    上传文件通常带不同 UUID，但从题库原文件重新识别时，两份文件可能来自不同目录
    却恰好同名。旧实现只按 stem 建目录，两路并发解析会同时替换同一个目录，结果可能
    把题干图片换成解析图片。仅在 stem 冲突时给解析侧加后缀，兼容常规路径的旧命名。
    """
    exam_scope = exam_path.stem
    solution_scope = solution_path.stem
    if exam_scope.casefold() == solution_scope.casefold():
        solution_scope += "_solution"
    return ((_raw_md_dir(exam_scope), exam_scope),
            (_raw_md_dir(solution_scope), solution_scope))


def _namespace_dual_images(markdown: str, extract_dir: Path,
                           side: str) -> str:
    """把双文件一侧的图片改成带来源前缀的唯一文件名。

    题干卷与答案卷由两个 OCR 任务独立解包，双方都很常见 ``images/1.png``。若直接
    拼 Markdown，后续无法知道某个 ``1.png`` 属于哪一侧；按目录先后拦截会把所有
    同名引用都绑定到第一侧。这里在合并文本前改名，随后可安全汇入一个图片目录。
    """
    images_dir = Path(extract_dir) / "images"
    mapped: dict[str, str] = {}

    def _move(raw_name: str) -> str | None:
        safe_name = Path(raw_name).name
        if safe_name in mapped:
            return mapped[safe_name]
        source = images_dir / safe_name
        if not source.is_file():
            # 进程可能在“图片已加前缀、Markdown 缓存尚未覆写”之间退出。
            # 原名缺失但目标名已存在时仍能恢复引用，无需重新 OCR。
            recovered = f"{side}_{safe_name}"
            if (images_dir / recovered).is_file():
                mapped[safe_name] = recovered
                return recovered
            return None
        # 失败后的整本 OCR 缓存会再次经过合并。已命名空间化的引用
        # 必须保持幂等，不能每重试一次就变成 exam_exam_...。
        if safe_name.startswith(f"{side}_"):
            mapped[safe_name] = safe_name
            return safe_name
        namespaced = f"{side}_{safe_name}"
        target = images_dir / namespaced
        if target.exists() and target != source:
            # 命名空间操作以复制方式保留原图；缓存重放时目标已存在是
            # 正常幂等状态，不再生成第二个摘要文件名。
            mapped[safe_name] = namespaced
            return namespaced
        if target != source and not target.exists():
            shutil.copy2(source, target)
        mapped[safe_name] = namespaced
        return namespaced

    def _replace(match: "re.Match") -> str:
        alt, raw_name = match.groups()
        namespaced = _move(raw_name)
        if namespaced is None:
            return match.group(0)
        return f"![{alt}](images/{namespaced})"

    markdown = _IMG_REF_RE.sub(_replace, markdown)

    def _replace_html(match: "re.Match") -> str:
        namespaced = _move(match.group(1))
        if namespaced is None:
            return match.group(0)
        whole = match.group(0)
        start, end = match.span(1)
        # span 是相对完整 Markdown 的坐标，换回当前匹配内部再精确替换 src 路径；
        # alt/title 恰好也含同名文件时不能先改错属性。
        offset = match.start()
        return (whole[:start - offset] + namespaced
                + whole[end - offset:])

    return _HTML_IMG_REF_RE.sub(_replace_html, markdown)


def _merge_dual_image_trees(exam_markdown: str, solution_markdown: str,
                            exam_dir: Path, solution_dir: Path):
    """命名空间化两侧图片，并把解析图片汇入题干工作区。"""
    exam_markdown = _namespace_dual_images(exam_markdown, exam_dir, "exam")
    solution_markdown = _namespace_dual_images(
        solution_markdown, solution_dir, "solution")
    exam_images = Path(exam_dir) / "images"
    solution_images = Path(solution_dir) / "images"
    if solution_images.is_dir():
        exam_images.mkdir(parents=True, exist_ok=True)
        solution_refs = _image_ref_counter(solution_markdown)
        for image_name in solution_refs:
            source = solution_images / Path(image_name).name
            if not source.is_file():
                continue
            target = exam_images / source.name
            # 保留解析侧缓存的自足性：若后续结构配对失败，重试应能直接
            # 复用这次付费 OCR，而不是因图片已被 move 走而再次识别整本。
            shutil.copy2(source, target)
    return exam_markdown, solution_markdown


# 切 CWD 用的互斥锁。绝对路径化之后，真正还需要 CWD 的只剩 project-alpha
# 的 load_config()——它走 dotenv.find_dotenv()，正常情况按调用栈的 __file__
# 找 .env（与 CWD 无关），但挂了调试器（sys.gettrace() 非 None）时会退化成
# 按 CWD 找。为这个边缘情况保留切换，同时用锁保证同一时刻只有一个线程处在
# 切换窗口里。这段只读 .env、不发网络，耗时微秒级，不影响并发收益。
_cwd_lock = threading.Lock()

@contextmanager
def _alpha_cwd():
    """临时把 CWD 切到 project-alpha 根，全程持锁，退出时切回。"""
    with _cwd_lock:
        prev_cwd = os.getcwd()
        os.chdir(config.PROJECT_ALPHA)
        try:
            yield
        finally:
            os.chdir(prev_cwd)

_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
_WORD_EXTS = {".docx", ".doc"}

# MinerU 对图片直传有单独的硬限制（实测报 file size exceeds limit(10MB)，
# 文档只公开写了 PDF/Word 200MB 的上限，图片这条线没写清楚，且不受本项目
# 控制）。留安全余量，图片一旦超过此阈值就先在本地转成单页 PDF 再走 PDF
# 上传通道（200MB 上限），从而让「大图片」也能传到远超 10MB。
_IMAGE_DIRECT_LIMIT_BYTES = 8 * 1024 * 1024


# project-alpha 的 load_config() 硬性要求 MINERU_API_TOKEN / DEEPSEEK_API_KEY 非空，
# 而软件版这两项都由自己的加密存储接管（mineru_store / providers）。校验前往环境里
# 补这个占位串把它糊过去，再按「取到的值是不是这个占位串」判断到底有没有真配置。
_ENV_PLACEHOLDER = "placeholder-injected-by-quizforge"


class ConvertError(Exception):
    """转换失败。"""


class CollectionRecognitionError(ConvertError):
    """整本 OCR/结构分组失败，并携带可安全复用及回收的缓存目录。"""

    def __init__(self, message: str, workspace_dirs=()):
        super().__init__(message)
        self.workspace_dirs = tuple(str(path) for path in workspace_dirs)


def is_word_file(filename: str) -> bool:
    return Path(filename).suffix.lower() in _WORD_EXTS


def _docx_images_in_order(docx_path: Path):
    """按 document.xml 里的出场顺序列出 docx 内嵌图片的 zip 条目名。

    **不能按 `word/media/` 的文件名排序**：那是字典序，`image10` 会排在 `image2`
    前面，12 张图的卷子直接乱页。真实顺序只有 `document.xml` 里 `<a:blip r:embed>`
    的出现次序说得准，rId 再经 `document.xml.rels` 映射到 media 文件名。

    读不出关系表或 XML 时返回空列表，调用方退回 pandoc 正常路径。
    """
    import zipfile

    try:
        with zipfile.ZipFile(docx_path) as z:
            names = set(z.namelist())
            if "word/document.xml" not in names:
                return []
            doc = z.read("word/document.xml").decode("utf-8", "ignore")
            rels_name = "word/_rels/document.xml.rels"
            rels = (z.read(rels_name).decode("utf-8", "ignore")
                    if rels_name in names else "")
    except (OSError, zipfile.BadZipFile, KeyError):
        return []
    rid2target = dict(re.findall(r'Id="([^"]+)"[^>]*?Target="([^"]+)"', rels))
    out = []
    for rid in re.findall(r'<a:blip[^>]*?r:embed="([^"]+)"', doc):
        target = rid2target.get(rid)
        if not target:
            continue
        entry = "word/" + target.lstrip("/").replace("\\", "/")
        if entry in names and entry not in out:
            out.append(entry)
    return out


def _docx_has_text(docx_path: Path) -> bool:
    """docx 正文里是否有真实文字（`<w:t>` 节点或 OMML 公式）。

    判据只看 `word/document.xml`：页眉页脚里的「第 X 页」不算正文，有它也仍是扫描件。
    读不出时保守返回 True——让 pandoc 照常跑，不去猜。
    """
    import zipfile

    try:
        with zipfile.ZipFile(docx_path) as z:
            doc = z.read("word/document.xml").decode("utf-8", "ignore")
    except (OSError, zipfile.BadZipFile, KeyError):
        return True
    if "<m:oMath" in doc:
        return True
    return any(t.strip() for t in re.findall(r"<w:t[^>]*>([^<]*)</w:t>", doc))


def _image_only_docx_to_pdf(docx_path: Path, out_path: Path) -> Path | None:
    """纯图片 docx（扫描件/截图拼的卷子）→ 每图一页、原尺寸的 PDF；不适用返回 None。

    **这条路必须绕开 pandoc**（2026-08-07 在「2026年6月慈溪市高二期末测试数学全解析
    .docx」上定位到的）：那份卷子是 12 张 1709×2418 的整页截图、正文零文字，pandoc
    走 LaTeX 时把每张图当行内元素塞进 ctexart 的文字流，12 张图挤进 2 页 letter
    （612×792pt），每张被缩到 484×695pt —— 12 页内容压成 2 页、单张缩到原尺寸的
    28%。MinerU OCR 这种糊图只认得出前 3 道题，用户看到的「只识别出 3 道题」就是
    这么来的。pandoc 的 `-V geometry` / `--dpi` 都调不动这个，因为问题不是页面边距
    而是「图片进了文字流、还按 letter 分页」。

    改成直接抽图 + images_to_pdf：每张图独占一页、页面尺寸等于图片像素尺寸，
    零重采样。MinerU 拿到的就是原始分辨率的 12 页 PDF。

    只在「有内嵌图 且 正文无文字」时接管——图文混排的 docx 仍归 pandoc，那种卷子
    的文字得靠 LaTeX 排版，抽图会把文字全丢掉。
    """
    if not _docx_has_text(docx_path):
        entries = _docx_images_in_order(docx_path)
    else:
        entries = []
    if not entries:
        return None

    import tempfile
    import zipfile

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmpdir = Path(tempfile.mkdtemp(prefix="docximg_"))
    try:
        paths = []
        with zipfile.ZipFile(docx_path) as z:
            for i, entry in enumerate(entries):
                # 序号前缀保出场顺序，后缀保原扩展名（Pillow 靠它选解码器）
                dst = tmpdir / f"{i:04d}_{Path(entry).name}"
                dst.write_bytes(z.read(entry))
                paths.append(dst)
        images_to_pdf(paths, out_path)
    except (OSError, zipfile.BadZipFile, KeyError, ConvertError) as e:
        logger.warning("[WARN] 纯图 docx 直转 PDF 失败，回退 pandoc: %s: %s",
                       docx_path, e)
        return None
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    logger.info("[OK] 纯图 docx（%d 张图、正文无文字）已按每图一页转为 PDF: %s",
                len(entries), out_path)
    return out_path


def _docx_to_pdf(docx_path: Path, out_dir: Path) -> Path:
    """把 .docx 转成同目录下同名 .pdf（pandoc → xelatex，中文走 ctexart 文档类）。

    与 project-alpha 的 _ensure_pdf 不同：那个用 Windows Word COM 自动化，
    只能在装了 MS Word 的 Windows 机器上跑，生产环境（Ubuntu）用不了。
    这里改用 pandoc + xelatex——两边（本机 Windows 开发机、线上 Ubuntu 服务器）
    都已装好（config.PANDOC / config.XELATEX，导出试卷 PDF 也用的这套），
    不必再装 LibreOffice 之类的新依赖。

    **纯图片 docx 先被 _image_only_docx_to_pdf 截走**，不走 pandoc——理由见那个
    函数的 docstring（pandoc 会把 12 页截图压成 2 页并缩到 28%，OCR 只剩 3 道题）。

    .doc（旧版二进制格式）pandoc 读不了，直接报错提示另存为 .docx。
    """
    docx_path = Path(docx_path)
    if docx_path.suffix.lower() == ".doc":
        raise ConvertError("暂不支持旧版 .doc 格式，请用 Word/WPS 另存为 .docx 后重新上传")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{docx_path.stem}_word_input.pdf"
    direct = _image_only_docx_to_pdf(docx_path, out_path)
    if direct is not None:
        return direct
    # 输出只写 OCR 中间目录。先移除上轮中间件，避免 Pandoc 异常退出时误把旧 PDF
    # 当成本轮结果；绝不触碰 docx 旁可能存在的同名真实 PDF。
    out_path.unlink(missing_ok=True)
    cmd = [config.PANDOC, str(docx_path), "-o", str(out_path),
           "--pdf-engine", config.XELATEX, "-V", "documentclass=ctexart"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except FileNotFoundError as e:
        raise ConvertError(f"转换工具未找到: {e}") from e
    except subprocess.TimeoutExpired:
        raise ConvertError("Word 转 PDF 超时")
    if proc.returncode != 0 or not out_path.is_file():
        raise ConvertError(f"Word 转 PDF 失败: {proc.stderr[-500:]}")
    return out_path


def _ensure_src_on_path():
    """把 project-alpha 根加入 sys.path，使其 `src` 包可 import。"""
    root = config.PROJECT_ALPHA
    if root not in sys.path:
        sys.path.insert(0, root)


def _load_config_for_user(mineru_token: str = "", *, require_mineru: bool = True):
    """读 project-alpha 的集中配置（MinerU token、DeepSeek key 等）。

    单机单人版：每个 OCR 文档任务由 ocr_pool 从设置页多份 token 中取最空闲者，
    取不到才回落 `vendor/project_alpha/.env` 里的 `MINERU_API_TOKEN`。服务器版
    这里强制要求调用方传入用户自己的 token（多人共用一个 MinerU 账号会排队），
    本地没有别人，所以留空是正常路径而不是错误。

    **调 load_config() 前必须先给两个键补占位值**（照服务器版
    `_load_config_for_user` 的做法）：它会校验 `MINERU_API_TOKEN` 与
    `DEEPSEEK_API_KEY` 都非空、缺一个就抛 `ConfigError("请在 .env 中填写")`，
    可这两项在软件版**根本不该要求**——MinerU token 走 mineru_store 加密存储、
    下一行就被 dataclasses.replace 换掉；LLM 走 providers.json，`deepseek_api_key`
    只在 provider 为 None 的老回落分支才用得上。不补占位的话，用户在设置页明明
    填好了 token，一点转换却收到「缺少 MINERU_API_TOKEN，请在 .env 中填写」，
    指向一个他不需要碰的文件。只在缺失时补，不覆盖真实 .env 配置。
    """
    from src.config import load_config

    for key in ("MINERU_API_TOKEN", "DEEPSEEK_API_KEY"):
        if not os.environ.get(key, "").strip():
            os.environ[key] = _ENV_PLACEHOLDER

    cfg = load_config()
    if mineru_token and mineru_token.strip():
        return dataclasses.replace(cfg, mineru_token=mineru_token.strip())
    # 走到这里说明设置页一份 token 都没存。此时 cfg.mineru_token 要么是真 .env
    # 里的值（老用法，可用），要么就是上面那个占位串（等于没配）。
    if (require_mineru and not mineru_store.has_token()
            and (cfg.mineru_token or "").strip() == _ENV_PLACEHOLDER):
        raise ConvertError(
            "尚未配置 MinerU token，请在「设置」页填入，"
            "或写进 vendor/project_alpha/.env 的 MINERU_API_TOKEN")
    return cfg


def _make_llm_client(cfg, provider):
    """规范化用的 LLM 客户端。

    provider 有值 → 用 QuizForge 自己的 LLMClient（可换服务商、max_tokens 可配，
    修掉了推理模型思维链吃光 8192 预算导致「返回空内容」的问题）。
    provider 为 None → 回落 project-alpha 的 DeepSeekClient（老行为）。
    两者都满足 normalize() 需要的鸭子接口 chat(system, user) -> (content, finish_reason)。
    """
    if provider is not None:
        logger.info("规范化使用 LLM 配置: %s model=%s max_tokens=%s",
                    provider.label, provider.model, provider.max_tokens)
        return llm_client.build_client(provider)

    from src.deepseek_client import DeepSeekClient
    # 老回落路径要的是 .env 里真实的 DeepSeek key。_load_config_for_user 会给缺失
    # 的键补占位串（否则 load_config 直接抛「请在 .env 中填写」），所以这里必须自己
    # 认出占位串——否则拿它去请求，报的会是一个看不懂的 401，而真正的原因是
    # 「既没在设置页配 LLM，.env 里也没有 key」。
    key = (cfg.deepseek_api_key or "").strip()
    if not key or key == _ENV_PLACEHOLDER:
        raise ConvertError(
            "尚未配置用于规范化的大模型：请在「设置」页添加一个 LLM 配置并启用，"
            "或在 vendor/project_alpha/.env 里填 DEEPSEEK_API_KEY")
    logger.info("规范化使用 project-alpha 默认 DeepSeek: model=%s", cfg.deepseek_model)
    return DeepSeekClient(key, cfg.deepseek_model)


def is_image_file(filename: str) -> bool:
    return Path(filename).suffix.lower() in _IMAGE_EXTS


def _oversized_image_to_pdf(file_path: Path, out_dir: Path) -> Path | None:
    """图片超过 MinerU 直传限制时，转成 OCR 中间目录的单页 PDF 并返回；
    未超限返回 None。转换失败必须显式失败：同一张已经确认损坏的图片继续直传，
    只会把错误推迟到 OCR 服务端，还可能得到“成功但空白”的误导结果。

    不能使用 ``file_path.with_suffix('.pdf')``：文件夹原卷“重新转换”会直接传题库
    中的真实图片，同目录若已有同名 PDF 就会被无提示覆盖；普通上传也会留下未登记
    的 PDF。预处理产物只允许落在可清理的 raw_md 中间目录。
    """
    try:
        if file_path.stat().st_size <= _IMAGE_DIRECT_LIMIT_BYTES:
            return None
    except OSError:
        return None
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{file_path.stem}_image_input.pdf"
    images_to_pdf([file_path], out_path)
    logger.info("[OK] 图片超过直传限制(%d MB)，已转为 PDF: %s",
               _IMAGE_DIRECT_LIMIT_BYTES // (1024 * 1024), out_path)
    return out_path


def images_to_pdf(image_paths, out_path) -> Path:
    """把多张图片按给定顺序合成一个 PDF（每张一页），返回 out_path。

    用途：一份卷子被拍成多张照片时，合成一个 PDF 再走现有 PDF 转换链路
    （图片拦截 / 题号过滤 / 解析关联全部复用），避免逐图分转导致题号重复。
    - exif_transpose：按手机照片的 EXIF 方向摆正，避免横拍图倒置。
    - 普通 JPEG 直接把原始 JPEG 字节嵌入 PDF，不做第二次有损编码，也不膨胀照片。
    - 其余格式按白底转 RGB，再用 PDF 的 FlateDecode 无损封装。Pillow 自带 PDF
      writer 会把 RGB 强制转成默认质量的 JPEG（DCTDecode），截图里的细笔画会在送
      OCR 前被二次有损压缩；这里直接写标准 PDF image XObject，且不缩放像素。
    任意一张打不开就取消整组合成，绝不跳页后继续识别；输出先写同目录临时文件，
    完整写成后再原子替换，写入中途失败也不会留下可被误用的半成品。
    """
    from PIL import Image, ImageOps

    pages = []
    for p in image_paths:
        path = Path(p)
        try:
            # JPEG 走原始压缩流直嵌时不会在写 PDF 阶段解码，因此先完整 verify；
            # 否则一张截断照片可能直到 OCR 服务端才暴露，且批次会误以为已合成。
            with Image.open(path) as source:
                source.verify()
            with Image.open(path) as source:
                orientation = source.getexif().get(274, 1)
                # 朝向正常的 JPEG 已经是 OCR 能直接消费的压缩流；原样嵌入，既无
                # 二次损失，也避免照片解压后再 Flate 导致 PDF 体积成倍增长。
                if (source.format == "JPEG" and orientation in (None, 1)
                        and source.mode in ("RGB", "L")):
                    pages.append((source.width, source.height,
                                  "DeviceRGB" if source.mode == "RGB" else "DeviceGray",
                                  "DCTDecode", path, True))
                    continue

                oriented = ImageOps.exif_transpose(source)   # 摆正手机横拍/竖拍
                pages.append((oriented.width, oriented.height, "DeviceRGB",
                              "FlateDecode", path, False))
                if oriented is not source:
                    oriented.close()
        except Exception as e:
            logger.warning("[WARN] 图片无法读取，已取消整组合成 %s: %s", path, e)
            raise ConvertError(
                f"图片 {path.name} 无法读取，已取消整组合成；请替换这张图片后重试"
            ) from e
    if not pages:
        raise ConvertError("没有可合成的图片")

    out_path = Path(out_path)
    import tempfile

    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
                prefix=f".{out_path.name}.", suffix=".tmp",
                dir=out_path.parent, delete=False) as temp_file:
            temp_path = Path(temp_file.name)
        _write_image_pdf(pages, temp_path)
        os.replace(temp_path, out_path)
    except Exception as e:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError as cleanup_error:
                logger.warning("[WARN] 清理图片 PDF 半成品失败 %s: %s",
                               temp_path, cleanup_error)
        if isinstance(e, ConvertError):
            raise
        raise ConvertError(f"图片合成 PDF 失败：{e}") from e
    logger.info("[OK] 合成 %d 张图片 -> %s", len(pages), out_path)
    return out_path


def _write_image_pdf(pages, out_path: Path) -> None:
    """把准备好的图片流写成一图一页 PDF，不依赖额外 PDF 库。

    页面仍沿用 Pillow 旧实现的 72 dpi 口径：MediaBox 的宽高等于像素宽高，因此
    替换编码方式不会改变送往 MinerU/Doc2X 的页面比例或坐标。图片流只用 PDF 1.4
    标准的 DCTDecode／FlateDecode；交叉引用表按实际字节偏移生成。
    """
    page_ids = [3 + index * 3 for index in range(len(pages))]
    object_count = 2 + len(pages) * 3
    offsets = [0] * (object_count + 1)

    with Path(out_path).open("wb") as output:
        output.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")

        def begin_object(number: int) -> None:
            offsets[number] = output.tell()
            output.write(f"{number} 0 obj\n".encode("ascii"))

        def write_object(number: int, body: bytes) -> None:
            begin_object(number)
            output.write(body)
            output.write(b"\nendobj\n")

        write_object(1, b"<< /Type /Catalog /Pages 2 0 R >>")
        kids = b" ".join(f"{number} 0 R".encode("ascii") for number in page_ids)
        write_object(
            2,
            (f"<< /Type /Pages /Count {len(page_ids)} /Kids [".encode("ascii")
             + kids + b"] >>"),
        )

        for index, page in enumerate(pages):
            page_id = page_ids[index]
            image_id = page_id + 1
            content_id = page_id + 2
            width, height, color_space, decode_filter, source_path, direct = page
            write_object(
                page_id,
                (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {width} {height}] "
                 f"/Resources << /ProcSet [/PDF /ImageC] /XObject "
                 f"<< /Im0 {image_id} 0 R >> >> /Contents {content_id} 0 R >>"
                 ).encode("ascii"),
            )

            if direct:
                image_length = source_path.stat().st_size
                image_stream = source_path.open("rb")
            else:
                # 非 JPEG 页逐条压到临时流：批量几十张大图时不把每页压缩数据全留
                # 在 pages 列表，也不让 `image.tobytes()` 再复制一整张 RGB 位图。
                import tempfile
                from PIL import Image, ImageOps

                image_stream = tempfile.TemporaryFile()
                with Image.open(source_path) as source:
                    oriented = ImageOps.exif_transpose(source)
                    if (oriented.mode in ("RGBA", "LA")
                            or "transparency" in oriented.info):
                        rgba = oriented.convert("RGBA")
                        rgb = Image.new("RGB", rgba.size, "white")
                        rgb.paste(rgba, mask=rgba.getchannel("A"))
                        rgba.close()
                    else:
                        rgb = oriented.convert("RGB")
                    compressor = zlib.compressobj(level=6)
                    for top in range(0, rgb.height, 64):
                        stripe = rgb.crop((0, top, rgb.width,
                                           min(top + 64, rgb.height)))
                        image_stream.write(compressor.compress(stripe.tobytes()))
                        stripe.close()
                    image_stream.write(compressor.flush())
                    rgb.close()
                    if oriented is not source:
                        oriented.close()
                image_length = image_stream.tell()
                image_stream.seek(0)

            image_head = (
                f"<< /Type /XObject /Subtype /Image /Width {width} /Height {height} "
                f"/ColorSpace /{color_space} /BitsPerComponent 8 /Interpolate false "
                f"/Filter /{decode_filter} /Length {image_length} >>\nstream\n"
            ).encode("ascii")
            try:
                begin_object(image_id)
                output.write(image_head)
                shutil.copyfileobj(image_stream, output, length=1024 * 1024)
                output.write(b"\nendstream\nendobj\n")
            finally:
                image_stream.close()

            commands = f"q\n{width} 0 0 {height} 0 0 cm\n/Im0 Do\nQ\n".encode("ascii")
            content_head = f"<< /Length {len(commands)} >>\nstream\n".encode("ascii")
            write_object(content_id, content_head + commands + b"endstream")

        xref_offset = output.tell()
        output.write(f"xref\n0 {object_count + 1}\n".encode("ascii"))
        output.write(b"0000000000 65535 f \n")
        for number in range(1, object_count + 1):
            output.write(f"{offsets[number]:010d} 00000 n \n".encode("ascii"))
        output.write(
            (f"trailer\n<< /Size {object_count + 1} /Root 1 0 R >>\n"
             f"startxref\n{xref_offset}\n%%EOF\n").encode("ascii")
        )


# 识别引擎：
#   "whole" —— 老路径，整篇交给 project-alpha 的 normalize，块数由模型决定；
#   "block" —— 新路径，先机械切块再逐块判定（blockpipe），块数由代码定死。
# 两条路径并存、互不影响，默认仍走老路径。
ENGINE_WHOLE = "whole"
ENGINE_BLOCK = "block"

# OCR 服务与后续的拆题引擎是两条正交维度：Doc2X/MinerU 决定原始 Markdown
# 从哪里来，whole/block 决定拿到原文后怎么切题和规范化。
OCR_MINERU = "mineru"
OCR_DOC2X = "doc2x"
OCR_BACKENDS = (OCR_MINERU, OCR_DOC2X)


def normalize_ocr_backend(raw: str) -> str:
    return OCR_DOC2X if (raw or "").strip().lower() == OCR_DOC2X else OCR_MINERU


def _normalize_image_page_count(raw) -> int:
    """旧任务没有页数字段，非法值也只能按普通单文件处理。"""
    try:
        return max(0, int(raw or 0))
    except (TypeError, ValueError):
        return 0


def _content_row_anchor_groups(row: dict) -> list[list[str]]:
    """按渲染先后提取 MinerU 内容块在 Markdown 中可能留下的锚点。"""
    def _strings(*values) -> list[str]:
        output: list[str] = []
        for value in values:
            items = value if isinstance(value, list) else [value]
            for item in items:
                if not isinstance(item, str):
                    continue
                text = item.strip()
                if text and text not in output:
                    output.append(text)
        return output

    kind = str(row.get("type") or "")
    image_path = row.get("img_path")
    image_anchor = []
    if isinstance(image_path, str) and image_path.strip():
        image_anchor = [f"images/{Path(image_path).name}"]
    if kind == "table":
        # MinerU 先写表题，再在 HTML 表体和表格截图中择一输出，最后写脚注。
        groups = [
            _strings(row.get("table_caption")),
            _strings(row.get("table_body"), *image_anchor),
            _strings(row.get("table_footnote")),
        ]
    elif kind in {"image", "chart"}:
        groups = [
            image_anchor,
            _strings(row.get("image_caption")),
            _strings(row.get("image_footnote")),
        ]
    else:
        # list_items 的第一个项目才是块首；后续项目唯一命中也不能证明页界。
        list_items = _strings(row.get("list_items"))
        first_list_item = list_items[:1]
        groups = [_strings(
            row.get("text"), row.get("content"), *first_list_item)]
    return [group for group in groups if group]


def _anchor_occurrences(text: str, anchor: str) -> list[int]:
    """返回精确锚点的全部位置；页界证据只接受恰好一处。"""
    positions: list[int] = []
    cursor = 0
    while True:
        position = text.find(anchor, cursor)
        if position < 0:
            return positions
        positions.append(position)
        cursor = position + max(1, len(anchor))


def _anchor_is_strong(anchor: str) -> bool:
    """短标签即使全篇唯一也不足以证明页首，宁可转人工校对。"""
    if anchor.startswith("images/") or anchor.lstrip().startswith("<table"):
        return True
    visible = re.sub(r"[\s`*_#$\\{}<>]+", "", anchor)
    return len(visible) >= 4


def _locate_page_start(raw_md: str, rows: list[dict]
                       ) -> tuple[int | None, str]:
    """定位本页首个实际内容块；不越过无法定位的块尝试后续正文。"""
    for row in rows:
        groups = _content_row_anchor_groups(row)
        if not groups:
            continue
        for group in groups:
            occurrences = {
                anchor: _anchor_occurrences(raw_md, anchor)
                for anchor in group
            }
            if not any(occurrences.values()):
                # 表题或图片载荷可能未被输出，可继续检查同一块的下一种载荷。
                continue
            hits = [positions[0]
                    for anchor, positions in occurrences.items()
                    if len(positions) == 1 and _anchor_is_strong(anchor)]
            if not hits:
                return None, "首个内容块在 Markdown 中重复或锚点过短"
            position = min(hits)
            return raw_md.rfind("\n", 0, position) + 1, ""
        return None, "首个内容块未进入 Markdown 或无法精确对应"
    return None, "没有可定位的可见内容"


def _inject_source_page_breaks(raw_md: str, extract_dir: Path,
                               image_page_count: int
                               ) -> tuple[str, int, str]:
    """用最终 MinerU content_list 为多图片合成 PDF 注入可靠页界。

    这里只接受三项同时成立的证据：content_list 页数与上传图片数一致、页码按
    0..N-1 连续出现、每个下一页的首个可见块能在同轮 Markdown 中唯一且顺序
    定位。任一项不成立都返回原文与原因，调用方据此暂停免审，绝不猜测位置。
    """
    import blocksplit

    count = _normalize_image_page_count(image_page_count)
    if count <= 1:
        return raw_md, 0, ""
    candidates = [
        path for path in Path(extract_dir).glob("*_content_list.json")
        if not path.name.endswith("_content_list_v2.json")
    ]
    if not candidates:
        return raw_md, 0, "未找到最终轮次的 content_list.json"
    try:
        latest = max(candidates, key=lambda path: path.stat().st_mtime_ns)
        rows = json.loads(latest.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return raw_md, 0, f"content_list.json 无法读取：{exc}"
    if not isinstance(rows, list) or not rows:
        return raw_md, 0, "content_list.json 没有页面内容"

    page_rows: dict[int, list[dict]] = {}
    page_order: list[int] = []
    for row in rows:
        if not isinstance(row, dict):
            return raw_md, 0, "content_list.json 含无效内容块"
        page = row.get("page_idx")
        if not isinstance(page, int) or page < 0:
            return raw_md, 0, "content_list.json 缺少合法页码"
        page_order.append(page)
        page_rows.setdefault(page, []).append(row)
    if any(right < left for left, right in zip(page_order, page_order[1:])):
        return raw_md, 0, "content_list.json 页码顺序逆序"
    expected_pages = list(range(count))
    if sorted(page_rows) != expected_pages:
        actual = ",".join(str(page + 1) for page in sorted(page_rows)) or "无"
        return raw_md, 0, (
            f"content_list.json 页面为 {actual}，与上传的 {count} 张图片不一致")

    page_positions: list[int] = []
    previous = -1
    # 第 1 页也必须定位：否则“第 2 页锚点误命中文首”的错误仍会通过单调性检查。
    # 页眉、页脚和页码若实际进入 Markdown，同样属于必须在页界后丢弃的游离文字，
    # 不能跳过；首个实际块无法唯一定位时整份转人工校对。
    for page in range(count):
        position, error = _locate_page_start(raw_md, page_rows[page])
        if position is None:
            return raw_md, 0, f"第 {page + 1} 页{error}"
        if position <= previous:
            return raw_md, 0, f"第 {page + 1} 页页界在 Markdown 中逆序或重复"
        page_positions.append(position)
        previous = position

    marker = blocksplit.SOURCE_PAGE_BREAK
    # 从后往前插入，前面已确定的位置不会被后续插入偏移。
    output = raw_md
    for position in reversed(page_positions[1:]):
        output = output[:position] + marker + "\n" + output[position:]
    return output, len(page_positions) - 1, ""


def _apply_image_page_boundaries(raw_md: str, extract_dir: Path, *,
                                 boundary_mode: str, image_page_count: int,
                                 ocr_backend: str, note_sink=None,
                                 label: str = "") -> tuple[str, dict]:
    """按白名单模式需要应用多图片分页隔离，并返回可留档的证据状态。"""
    mode = normalize_boundary_mode(boundary_mode)
    count = _normalize_image_page_count(image_page_count)
    if mode != BOUNDARY_WHITELIST or count <= 1:
        return raw_md, {}

    prefix = f"（{label}）" if label else ""
    meta = {"image_page_count": count}
    backend = normalize_ocr_backend(ocr_backend)
    if backend != OCR_MINERU:
        error = "Doc2X 当前不提供可与 Markdown 可靠对应的页界坐标"
        separated, inserted = raw_md, 0
    else:
        separated, inserted, error = _inject_source_page_breaks(
            raw_md, extract_dir, count)
    if error:
        meta.update(source_page_boundary_status="unavailable",
                    source_page_break_count=0)
        note = qualcheck.mark_manual_review(
            f"{prefix}{count} 张图片合成后的分页隔离失败（{error}），已保留原文；"
            "请在拆题校对页逐页核对首尾内容，避免相邻图片文字串入同一道题")
        logger.warning("[WARN] %s", note)
        if note_sink is not None:
            note_sink(note)
        return raw_md, meta

    meta.update(source_page_boundary_status="reliable",
                source_page_break_count=inserted)
    logger.info("[OK] %s已根据 MinerU 页面证据隔离 %d 张上传图片的 %d 处页界",
                prefix, count, inserted)
    return separated, meta


def _check_options(raw_md: str, note_sink, *, label: str = "") -> None:
    """扫 MinerU 原文里「只剩选项标签、没有选项内容」的行，有则经 note_sink 告警。

    放在这里而不是 blockpipe 里，是因为这类丢失发生在 MinerU 的识别阶段，两条
    识别引擎（whole / block）都会中招——blockpipe 只在 ENGINE_BLOCK 路径上跑。
    检测点必须在拿到原文之后、交给 LLM 之前：LLM 补不出丢的内容，也不会报错，
    过了那一步就再没有人知道这份卷子缺过东西（详见 optcheck 模块文档）。

    label 用于双文件路径区分是题干还是解析那份。
    检测本身不改正文、不阻断转换：告警是给用户的校对线索，不是失败。
    """
    if note_sink is None:
        return
    try:
        gaps = optcheck.find_empty_options(raw_md)
    except Exception as e:  # 检测是辅助功能，自己出问题绝不能拖垮转换
        logger.warning("[WARN] 选项完整性检测失败（不影响转换）: %s", e)
        return
    if not gaps:
        return
    note = optcheck.build_note(gaps)
    if label:
        note = f"（{label}）{note}"
    logger.warning("[WARN] 选项疑似缺失 %d 处: %s", len(gaps),
                   "、".join(g.describe() for g in gaps))
    note_sink(qualcheck.mark_manual_review(note))


def _repair_choice_images(raw_md: str, extract_dir: Path, note_sink=None,
                          *, label: str = "") -> str:
    """用 MinerU 的 bbox 恢复“题干图 + A-D 各一图”的二维归属。"""
    try:
        # 页界是白名单切题的内部控制标记，不能让图片坐标修复把它并入题块后
        # 重排或丢掉。逐页修复再原位拼回，既保护页界，也杜绝跨页移动图片。
        import blocksplit
        marker = blocksplit.SOURCE_PAGE_BREAK
        parts = raw_md.split(marker)
        repaired_parts = []
        count = 0
        for part in parts:
            repaired_part, repaired_count = imgorder.repair_document(
                part, extract_dir)
            repaired_parts.append(repaired_part)
            count += repaired_count
        repaired = marker.join(repaired_parts)
    except Exception as e:
        # 坐标修复是增强档；任何第三方 JSON 形态变化都只能降级为原 Markdown，
        # 不能让原本可导入的试卷整体失败。
        logger.warning("[WARN] 多图选择题坐标恢复失败（已保留原文）: %s", e)
        return raw_md
    if count:
        prefix = f"（{label}）" if label else ""
        message = (f"{prefix}检测到 {count} 道多图选择题，已按页面坐标区分题干图"
                   "与 A-D 选项图")
        logger.info("[OK] %s", message)
        if note_sink is not None:
            note_sink(message)
    return repaired


def _has_text_beyond_images(raw_md: str) -> bool:
    """原文里除了图片引用之外还有没有真东西。

    **不能只用 `strip()` 判空**：坏 PDF 的真实产物不是空串，而是两行
    `![](images/<sha>.jpg)` 加一个空行——strip() 之后非空，一路放行到最后变成
    「已就绪但点不开」的空组。所以先按 `_IMG_REF_RE` 剔掉图片引用（连文件名里的
    sha 一起消失，否则那串十六进制自己就是「字」），再看剩下还有没有字母或数字：
    只剩 `#`、标点、空白的一律算没内容。
    """
    text = _HTML_IMG_REF_RE.sub(" ", _IMG_REF_RE.sub(" ", raw_md))
    return any(ch.isalnum() for ch in text)


_MOJIBAKE_REDECODE = ("cp1252", "latin-1")


def _repair_mojibake(text: str) -> str:
    """把「UTF-8 字节被误当单字节编码解码」这种乱码尝试撞回原文。

    只在两个方向都严格可逆时才生效：先把已经解出来的字符按 cp1252/latin-1
    编回字节——真乱码的字符范围都落在 0-255 内，这一步不会报错；真正的中文字符
    编码超出这个范围，`encode()` 直接抛异常，函数原样返回，绝不会误伤正常文本。
    再按 UTF-8 解码那些字节，解不出合法 UTF-8 就说明猜错了方向，同样原样返回。
    两步都成功且结果与原文不同才采用——普通英文/半角标点走这两步是恒等变换，
    `candidate != text` 挡掉这类空操作。
    """
    for wrong_enc in _MOJIBAKE_REDECODE:
        try:
            candidate = text.encode(wrong_enc).decode("utf-8")
        except (UnicodeDecodeError, UnicodeEncodeError):
            continue
        if candidate != text:
            return candidate
    return text


def _clean_mineru_text(raw_md: str, path: Path, *, label: str = "",
                       note_sink=None) -> str:
    """MinerU 原文接收后的第一步清洗：撞回可逆的乱码，标记不可逆的替换符。

    vendor 的 mineru_client.py 对 zip 里的 md 用 `errors="replace"` 解码（按
    约定不改这个文件），一旦 MinerU 返回的字节本身编码有误，替换符 `\\ufffd`
    就会混进原文，且这一步已经丢了原始字节、程序侧再也补不回来——只能靠
    `_repair_mojibake` 先把「能撞对方向」的那类修好，剩下真丢了数据的用
    note_sink 告警给用户，而不是悄悄放过去变成看不出原因的乱码题。
    """
    fixed = _repair_mojibake(raw_md)
    if "�" in fixed:
        n = fixed.count("�")
        prefix = f"（{label}）" if label else ""
        logger.warning("[WARN] %s%s 原文含 %d 处无法还原的乱码字符",
                       prefix, path.name, n)
        if note_sink is not None:
            note_sink(qualcheck.mark_manual_review(
                f"{prefix}{path.name} 识别结果中有 {n} 处字符疑似乱码，"
                "请检查原文对应位置"))
    return fixed


def _strip_standalone_figure_captions(text: str) -> str:
    """消去两轮 OCR 可能时有时无的独占图注，不改正文中的同名字母。"""
    return re.sub(
        r"(?mi)^\s*(?:图\s*)?[A-Da-d甲乙丙丁]\s*[.．、:：]?\s*$",
        " ", text or "",
    )


def _visible_question_units(text: str) -> int:
    """统计题块里可供人阅读的正文单位，排除题号、图片路径和 Markdown 标记。"""
    value = _HTML_IMG_REF_RE.sub(" ", _IMG_REF_RE.sub(" ", text or ""))
    value = re.sub(r"<[^>]+>", " ", value)
    # a/b、甲/乙这类独占一行的图注只描述版面位置，不是题干正文。两轮 OCR
    # 可能把它留成文字，也可能随图片一起裁入；完整性比较不能因此出现 1 字波动。
    value = _strip_standalone_figure_captions(value)
    value = re.sub(
        r"^\s*(?:#{1,6}\s*)?(?:第\s*)?\d{1,3}\s*(?:[题題]|[.．、:：)])?",
        " ", value, count=1,
    )
    # 只计字母、数字和汉字；公式命令中的有效字母仍会计入，而括号、反斜杠、
    # Markdown 星号等版式字符不会把“3. A”这种空壳候选抬成完整正文。
    return sum(
        1 for char in value
        if char.isalnum() or "\u3400" <= char <= "\u9fff"
    )


def _collection_block_image_count(text: str) -> int:
    return sum(_image_ref_counter(text).values())


def _collection_blocks(unit, *, allow_out_of_order: bool = False):
    """取一个合集单元的有号题块；重复号始终拒绝，乱序仅供恢复链显式放行。"""
    import blocksplit

    blocks = [
        block for block in blocksplit.split_blocks(unit.markdown)
        if isinstance(block.number, int) and 1 <= block.number <= 300
    ]
    numbers = [block.number for block in blocks]
    if (len(numbers) < 2 or len(numbers) != len(set(numbers))
            or (not allow_out_of_order and numbers != sorted(numbers))):
        return None
    return blocks


def _collection_interstitial_heading(line: str) -> bool:
    """是否为机械切块会保留为上下文、但不会收入题块的结构行。"""
    import blocksplit

    body = blocksplit._strip_head(line)
    if not body:
        return False
    return bool(
        blocksplit._SEC_LINE_RE.match(body)
        or blocksplit._GRP_LINE_RE.match(body)
        or blocksplit._ANS_LINE_RE.match(body)
        or blocksplit._PRACTICE_SECTION_RE.match(body)
        or blocksplit._SECTION_MARK_RE.match(body)
        or blocksplit._TOC_LEADER_RE.search(body)
    )


def _block_span_end(markdown: str, start: int, limit: int) -> int:
    """从候选区间尾部剥离明确结构标题与空白，并返回正文终点。"""
    segment_lines: list[tuple[int, str]] = []
    line_offset = start
    for line in markdown[start:limit].splitlines(keepends=True):
        segment_lines.append((line_offset, line))
        line_offset += len(line)

    structural_start: int | None = None
    for line_start, line in reversed(segment_lines[1:]):
        if not line.strip():
            continue
        if _collection_interstitial_heading(line):
            structural_start = line_start
            continue
        break
    end = structural_start if structural_start is not None else limit
    while end > start and markdown[end - 1].isspace():
        end -= 1
    return end


def _block_body_signature(text: str) -> str:
    """只消去机械补出的题号与空白；正文任一字符不同都不会等价。"""
    import blocksplit

    value = blocksplit._LEADING_QUESTION_NUMBER_RE.sub(
        "", (text or "").strip(), count=1)
    return "".join(value.split())


def _line_number_block_spans(
        markdown: str, blocks) -> dict[int, tuple[int, int]] | None:
    """用 blocksplit 保存的原始行号恢复被机械合成题块的连续来源区间。

    行号只是候选坐标，不是放宽凭据：每个改写块还必须与该区间在“只去题号和
    空白”后逐字相等，并且这段正文在整个单元中只出现一次。行号重复、倒退、
    跨越非连续来源或任一字符对不上都会失败关闭。
    """
    lines = (markdown or "").splitlines(keepends=True)
    if not lines:
        return None
    line_starts: list[int] = []
    offset = 0
    for line in lines:
        line_starts.append(offset)
        offset += len(line)

    line_numbers = [getattr(block, "line_no", None) for block in blocks]
    if (not line_numbers
            or any(not isinstance(number, int) or number < 1
                   or number > len(line_starts) for number in line_numbers)
            or line_numbers != sorted(set(line_numbers))):
        return None

    compact_markdown = "".join(markdown.split())
    spans: dict[int, tuple[int, int]] = {}
    for index, block in enumerate(blocks):
        start = line_starts[line_numbers[index] - 1]
        limit = (line_starts[line_numbers[index + 1] - 1]
                 if index + 1 < len(blocks) else len(markdown))
        end = _block_span_end(markdown, start, limit)
        if end <= start:
            return None

        body = block.text.strip()
        exact_start = markdown.find(body, start, end)
        if (exact_start >= 0
                and markdown.find(body, exact_start + 1, end) < 0):
            spans[block.number] = (exact_start, exact_start + len(body))
            continue

        source_signature = _block_body_signature(markdown[start:end])
        block_signature = _block_body_signature(body)
        if (not source_signature or source_signature != block_signature
                or compact_markdown.count(source_signature) != 1):
            return None
        spans[block.number] = (start, end)
    return spans


def _raw_question_block_spans(
        markdown: str, blocks) -> dict[int, tuple[int, int]] | None:
    """按原始题号行定位题块；题号序列有任何歧义都失败关闭。

    ``blocksplit`` 会安全地修复题号标点、合并被 OCR 拆散的选项或调整图片，
    因此它返回的题块不一定还能逐字出现在 MinerU 原文中。这里不反推正文，
    只接受原文中与切块结果数量、顺序、题号完全相同的一组顶层题号行。
    """
    expected = [getattr(block, "number", None) for block in blocks]
    if (not expected or any(not isinstance(number, int) for number in expected)
            or len(expected) != len(set(expected))):
        return None

    hits: list[tuple[int, int]] = []
    fenced = False
    offset = 0
    for line in (markdown or "").splitlines(keepends=True):
        stripped = line.lstrip()
        if stripped.startswith(("```", "~~~")):
            fenced = not fenced
        elif not fenced and not stripped.startswith("|"):
            number = collection_structure._question_number(line)
            if isinstance(number, int) and 1 <= number <= 300:
                hits.append((number, offset))
        offset += len(line)
    if [number for number, _ in hits] != expected:
        return None

    spans: dict[int, tuple[int, int]] = {}
    for index, (number, start) in enumerate(hits):
        limit = hits[index + 1][1] if index + 1 < len(hits) else len(markdown)
        end = _block_span_end(markdown, start, limit)
        if end <= start:
            return None
        spans[number] = (start, end)
    return spans


def _exact_block_spans(markdown: str, blocks) -> dict[int, tuple[int, int]] | None:
    """把切块结果反查回原 Markdown，必要时严格回落到原始题号边界。"""
    spans: dict[int, tuple[int, int]] = {}
    cursor = 0
    for block in blocks:
        body = block.text.strip()
        start = markdown.find(body, cursor)
        if start < 0 or markdown.find(body, start + 1) >= 0:
            return (_line_number_block_spans(markdown, blocks)
                    or _raw_question_block_spans(markdown, blocks))
        end = start + len(body)
        if start < cursor:
            return (_line_number_block_spans(markdown, blocks)
                    or _raw_question_block_spans(markdown, blocks))
        spans[block.number] = (start, end)
        cursor = end
    return spans


def _missing_number_is_located(number: int, primary_numbers: list[int],
                               alternate_numbers: list[int]) -> bool:
    """只补能被相邻连续题号唯一夹定的位置，不按大致范围猜题号。"""
    index = alternate_numbers.index(number)
    before = alternate_numbers[index - 1] if index else None
    after = (alternate_numbers[index + 1]
             if index + 1 < len(alternate_numbers) else None)
    if before is None:
        return number == 1 and after == 2 and after in primary_numbers
    if after is None:
        return (before == number - 1 and before in primary_numbers
                and number == max(alternate_numbers))
    return (before == number - 1 and after == number + 1
            and before in primary_numbers and after in primary_numbers)


def _alternate_block_is_sufficient(block) -> bool:
    """候选必须有可见正文且自身不含不可逆乱码，图片不能代替缺失题干。"""
    return "�" not in block.text and _visible_question_units(block.text) >= 10


def _choice_stem_signature(text: str) -> str:
    """选择题题干的严格机械签名，不含题号、图片与 A-D 选项。"""
    import mechfix

    value = mechfix.normalize_embedded_choice_labels(text or "")
    blanks = list(mechfix._EMPTY_ANSWER_PAREN_RE.finditer(value))
    blank = blanks[-1] if blanks else None
    labels = [match for match in mechfix._CHOICE_LABEL_RE.finditer(value)
              if blank is None or match.start() >= blank.end()]
    # 答题空是题干与选项之间最稳定的边界。整本结果把全部标签吞掉时，若继续把
    # 空后的裸公式算进签名，正确的局部候选反而会因新增 A—D 而被严格比对拒绝。
    if blank is not None:
        value = value[:blank.end()]
    elif labels:
        value = value[:labels[0].start()]
    value = _HTML_IMG_REF_RE.sub(" ", _IMG_REF_RE.sub(" ", value))
    value = _strip_standalone_figure_captions(value)
    value = re.sub(
        r"^\s*(?:#{1,6}\s*)?(?:第\s*)?\d{1,3}\s*"
        r"(?:[题題]|[.．、:：)])?", " ", value, count=1)
    return "".join(
        char.casefold() for char in value
        if char.isalnum() or "\u3400" <= char <= "\u9fff"
    )


def _alternate_block_is_better(primary, alternate, *,
                                require_matching_stem: bool = False) -> bool:
    """同号题仅在候选明确增益且正文、图片均不缩水时替换。"""
    import mechfix

    if not _alternate_block_is_sufficient(alternate):
        return False
    primary_units = _visible_question_units(primary.text)
    alternate_units = _visible_question_units(alternate.text)
    primary_images = _collection_block_image_count(primary.text)
    alternate_images = _collection_block_image_count(alternate.text)
    if alternate_units < primary_units or alternate_images < primary_images:
        return False

    primary_choice = mechfix.has_complete_choice_options(
        mechfix.normalize_embedded_choice_labels(primary.text))
    alternate_choice = mechfix.has_complete_choice_options(
        mechfix.normalize_embedded_choice_labels(alternate.text))
    if require_matching_stem:
        primary_stem = _choice_stem_signature(primary.text)
        alternate_stem = _choice_stem_signature(alternate.text)
        primary_cjk = "".join(
            char for char in primary_stem if "\u3400" <= char <= "\u9fff")
        alternate_cjk = "".join(
            char for char in alternate_stem if "\u3400" <= char <= "\u9fff")
        exact_text = primary_stem == alternate_stem
        exact_cjk = (len(primary_cjk) >= 20
                     and primary_cjk == alternate_cjk)
        # 数学变量在同一页两次 MinerU 中可能写成 t_1 / t_I，不能因此拒掉已由
        # 唯一页与题号锁定的正确候选；中文题干必须逐字相同，仍会拦住“运动→静止”。
        if len(primary_stem) < 20 or not (exact_text or exact_cjk):
            return False
    choice_gain = alternate_choice and not primary_choice
    body_gain = alternate_units >= primary_units + max(16, primary_units // 4)
    repaired_garble = ("�" in primary.text and "�" not in alternate.text
                       and alternate_units >= primary_units)
    return choice_gain or body_gain or repaired_garble


def _image_references(text: str) -> list[tuple[int, int, str, str]]:
    """按原文坐标列出 Markdown/HTML 图片引用及路径，供无损局部移位。"""
    refs = [
        (match.start(), match.end(), match.group(2), match.group(0))
        for match in _IMG_REF_RE.finditer(text or "")
    ]
    refs.extend(
        (match.start(), match.end(), match.group(1), match.group(0))
        for match in _HTML_IMG_REF_RE.finditer(text or "")
    )
    return sorted(refs)


def _image_is_fully_outside_slices(
        box, slices: tuple[collection_recovery.PageSlice, ...]) -> bool:
    """仅当图片矩形与所有裁片严格分离时返回 True；贴边也算裁片内。"""
    try:
        image_top = float(box.bbox[1])
        image_bottom = float(box.bbox[3])
        page = int(box.page)
    except (AttributeError, TypeError, ValueError, IndexError):
        return False
    if not (0 <= image_top < image_bottom <= 1000):
        return False
    for part in slices:
        if part.page_index != page:
            continue
        slice_top = float(part.top) * 1000
        slice_bottom = float(part.bottom) * 1000
        if image_bottom >= slice_top and image_top <= slice_bottom:
            return False
    return True


def _layout_image_owner_number(layout_unit, box, *,
                               next_layout_unit=None) -> int | None:
    """按严格递增的题号区间，唯一确定图片完整落入的题目。"""
    questions = list(getattr(layout_unit, "questions", ()) or ())
    numbers = [getattr(question, "number", None) for question in questions]
    if (not questions or len(numbers) != len(set(numbers))
            or any(not isinstance(number, int) for number in numbers)
            or numbers != sorted(numbers)):
        return None
    try:
        starts = [
            (int(question.page_index), float(question.bbox[1]))
            for question in questions
        ]
        image_start = (int(box.page), float(box.bbox[1]) / 1000)
        image_end = (int(box.page), float(box.bbox[3]) / 1000)
    except (AttributeError, TypeError, ValueError, IndexError):
        return None
    # 两栏页的纵坐标可能倒退；没有横向阅读顺序证据时不能据此猜归属。
    if starts != sorted(starts) or len(starts) != len(set(starts)):
        return None
    owner_index = next(
        (index - 1 for index, start in enumerate(starts)
         if start > image_start),
        len(starts) - 1,
    )
    if owner_index < 0 or starts[owner_index] > image_start:
        return None
    if owner_index + 1 < len(starts):
        owner_end = starts[owner_index + 1]
    else:
        next_lines = list(getattr(next_layout_unit, "lines", ()) or ())
        if not next_lines:
            return None
        try:
            owner_end = min(
                (int(line.page_index), float(line.bbox[1]))
                for line in next_lines
            )
        except (AttributeError, TypeError, ValueError, IndexError):
            return None
    # 图片必须完整落在题号起点和下一题（或下一单元标题）之间；触边也拒绝。
    if image_end >= owner_end:
        return None
    return questions[owner_index].number


def _relocate_out_of_crop_images(
        text: str, *, question_number: int,
        plan: collection_recovery.GapCropPlan, source_layout, layout_unit,
        existing_numbers, next_layout_unit=None,
        raw_source_text: str | None = None,
        ) -> tuple[str, dict[int, list[str]]]:
    """把可证明属于后续题的裁片外图片从比较文本中剥离并返回归还清单。

    这里不按“候选少一张图也没关系”放宽完整性。每张被剥离的图片都必须同时
    满足三个独立证据：原 content_list 有坐标、矩形完全位于本次裁片外、标题单元
    内严格递增的题号起点能把它唯一归到当前单元中一道人为未删除的后续题。
    """
    if source_layout is None or layout_unit is None:
        return text, {}
    allowed = set(existing_numbers)
    removals: list[tuple[int, int]] = []
    relocated: dict[int, list[str]] = {}
    if raw_source_text is not None:
        from collections import Counter
        raw_refs = Counter(item[3] for item in _image_references(raw_source_text))
    else:
        raw_refs = None
    for start, end, path, whole in _image_references(text):
        # content_list 只记录 images/ 下的单文件名；任何子目录、转义或百分号
        # 都不能拿 basename 冒充坐标证据。
        if (Path(path).name != path or any(mark in path for mark in ("/", "\\", "%"))
                or path in (".", "..")):
            continue
        box = imgorder._layout_box(path, source_layout)
        if box is None or not _image_is_fully_outside_slices(box, plan.slices):
            continue
        owner = _layout_image_owner_number(
            layout_unit, box, next_layout_unit=next_layout_unit)
        if owner is None or owner <= question_number or owner not in allowed:
            continue
        if raw_refs is not None and raw_refs[whole] <= 0:
            continue
        if raw_refs is not None:
            raw_refs[whole] -= 1
        removals.append((start, end))
        relocated.setdefault(owner, []).append(whole)
    if not removals:
        return text, {}
    comparison = text
    for start, end in reversed(removals):
        comparison = comparison[:start] + comparison[end:]
    return comparison, relocated


def _prepare_alternate_images(selected_texts: dict[tuple[int, int], str],
                              alternate_dir: Path, primary_dir: Path):
    """改写所选文本层图片引用，并生成待复制清单；任一断图都拒绝本轮合并。"""
    source_root = Path(alternate_dir) / "images"
    target_root = Path(primary_dir) / "images"
    image_names: dict[str, tuple[str, Path, Path]] = {}

    def _target_for(raw_name: str):
        safe_name = Path(raw_name).name
        cached = image_names.get(safe_name)
        if cached is not None:
            return cached[0]
        source = source_root / safe_name
        if (not source.is_file() or source.is_symlink()
                or source.parent != source_root):
            raise ValueError(f"文本层候选引用的图片不存在或不安全：{safe_name}")
        digest = hashlib.sha256(source.read_bytes()).hexdigest()[:16]
        suffix = source.suffix.lower()
        if not re.fullmatch(r"\.(?:png|jpe?g|webp|gif|svg|bmp)", suffix):
            raise ValueError(f"文本层候选图片扩展名不受支持：{safe_name}")
        target_name = f"text_layer_{digest}{suffix}"
        target = target_root / target_name
        if target.exists():
            if (not target.is_file() or target.is_symlink()
                    or hashlib.sha256(target.read_bytes()).digest()
                    != hashlib.sha256(source.read_bytes()).digest()):
                raise ValueError(f"文本层候选图片目标发生冲突：{target_name}")
        image_names[safe_name] = (target_name, source, target)
        return target_name

    rewritten: dict[tuple[int, int], str] = {}
    try:
        for key, text in selected_texts.items():
            def _replace_md(match: "re.Match") -> str:
                alt, raw_name = match.groups()
                return f"![{alt}](images/{_target_for(raw_name)})"

            value = _IMG_REF_RE.sub(_replace_md, text)

            def _replace_html(match: "re.Match") -> str:
                whole = match.group(0)
                start, end = match.span(1)
                offset = match.start()
                return (whole[:start - offset]
                        + _target_for(match.group(1))
                        + whole[end - offset:])

            rewritten[key] = _HTML_IMG_REF_RE.sub(_replace_html, value)
    except (OSError, ValueError):
        return None
    return rewritten, [(source, target) for _, source, target
                       in image_names.values()]


def _rebuild_collection_unit(unit_markdown: str, primary_blocks,
                             primary_spans: dict[int, tuple[int, int]],
                             selected: dict[int, str]) -> str:
    """只替换题块正文；专题标题、分区标题及块间原始文字全部沿用强制 OCR。"""
    if not selected:
        return unit_markdown
    primary_numbers = [block.number for block in primary_blocks]
    if set(primary_spans) != set(primary_numbers):
        raise ConvertError("合集局部恢复的题块坐标不完整")
    previous_end = 0
    for number in primary_numbers:
        span = primary_spans.get(number)
        if (not isinstance(span, tuple) or len(span) != 2
                or not all(isinstance(value, int) for value in span)):
            raise ConvertError("合集局部恢复的题块坐标格式无效")
        start, end = span
        if not (previous_end <= start < end <= len(unit_markdown)):
            raise ConvertError("合集局部恢复的题块坐标越界或互相重叠")
        previous_end = end
    insert_before: dict[int, list[tuple[int, str]]] = {}
    append: list[tuple[int, str]] = []
    replacements: dict[int, str] = {}
    for number, text in selected.items():
        if number in primary_spans:
            replacements[number] = text.strip()
            continue
        later = [value for value in primary_numbers if value > number]
        if later:
            anchor = min(later)
            insert_before.setdefault(anchor, []).append((number, text.strip()))
        else:
            append.append((number, text.strip()))

    pieces: list[str] = []
    cursor = 0
    for block in primary_blocks:
        start, end = primary_spans[block.number]
        pieces.append(unit_markdown[cursor:start])
        additions = insert_before.get(block.number, [])
        if additions:
            pieces.append("\n\n".join(
                text for _, text in sorted(additions)) + "\n\n")
        pieces.append(replacements.get(block.number, unit_markdown[start:end]))
        cursor = end
    pieces.append(unit_markdown[cursor:])
    rebuilt = "".join(pieces)
    if append:
        rebuilt = rebuilt.rstrip() + "\n\n" + "\n\n".join(
            text for _, text in sorted(append))
    return rebuilt


def _merge_collection_ocr_variants(primary_markdown: str, primary_dir: Path,
                                   alternate_markdown: str,
                                   alternate_dir: Path):
    """以强制 OCR 为主，按合集单元和唯一题号机械吸收文本层明确更好的题块。

    返回 ``(Markdown, 补回数, 替换数)``；任何标题对应、题号顺序或原文定位不唯一
    时返回 ``None``，调用方继续使用既有整本择优。这里不推断题意、不生成正文。
    """
    try:
        primary_units = collection_structure.split_markdown_units(
            primary_markdown, label="强制 OCR 合集")
        alternate_units = collection_structure.split_markdown_units(
            alternate_markdown, label="文本层合集")
    except collection_structure.CollectionStructureError:
        return None
    if len(primary_units) != len(alternate_units):
        return None

    selected_texts: dict[tuple[int, int], str] = {}
    selected_numbers: list[dict[int, object]] = []
    primary_data = []
    inserted = 0
    replaced = 0
    for unit_index, (primary_unit, alternate_unit) in enumerate(
            zip(primary_units, alternate_units)):
        if not collection_structure._topics_compatible(
                primary_unit.topic, alternate_unit.topic):
            return None
        if (primary_unit.ordinal is not None and alternate_unit.ordinal is not None
                and primary_unit.ordinal != alternate_unit.ordinal):
            return None
        primary_blocks = _collection_blocks(primary_unit)
        alternate_blocks = _collection_blocks(alternate_unit)
        if primary_blocks is None or alternate_blocks is None:
            return None
        primary_spans = _exact_block_spans(primary_unit.markdown, primary_blocks)
        alternate_spans = _exact_block_spans(
            alternate_unit.markdown, alternate_blocks)
        if primary_spans is None or alternate_spans is None:
            return None

        primary_by_number = {block.number: block for block in primary_blocks}
        alternate_by_number = {block.number: block for block in alternate_blocks}
        primary_numbers = list(primary_by_number)
        alternate_numbers = list(alternate_by_number)
        chosen: dict[int, object] = {}
        for number, alternate in alternate_by_number.items():
            primary = primary_by_number.get(number)
            if primary is None:
                if (_alternate_block_is_sufficient(alternate)
                        and _missing_number_is_located(
                            number, primary_numbers, alternate_numbers)):
                    chosen[number] = alternate
                    inserted += 1
            elif _alternate_block_is_better(primary, alternate):
                chosen[number] = alternate
                replaced += 1
        for number, block in chosen.items():
            selected_texts[(unit_index, number)] = block.text.strip()
        selected_numbers.append(chosen)
        primary_data.append((primary_unit, primary_blocks, primary_spans))

    prepared = _prepare_alternate_images(
        selected_texts, Path(alternate_dir), Path(primary_dir))
    if prepared is None:
        return None
    rewritten, copy_plan = prepared

    rebuilt_units: list[str] = []
    for unit_index, (unit, blocks, spans) in enumerate(primary_data):
        selected = {
            number: rewritten[(unit_index, number)]
            for number in selected_numbers[unit_index]
        }
        rebuilt_units.append(_rebuild_collection_unit(
            unit.markdown, blocks, spans, selected))

    # 用强制 OCR 原文作骨架，逐个精确替换单元；封面、目录和单元间文字原样保留。
    output: list[str] = []
    cursor = 0
    for unit, rebuilt in zip(primary_units, rebuilt_units):
        start = primary_markdown.find(unit.markdown, cursor)
        if start < 0 or primary_markdown.find(unit.markdown, start + 1) >= 0:
            return None
        output.append(primary_markdown[cursor:start])
        output.append(rebuilt)
        cursor = start + len(unit.markdown)
    output.append(primary_markdown[cursor:])
    merged = "".join(output)

    created: list[Path] = []
    try:
        for source, target in copy_plan:
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                shutil.copy2(source, target)
                created.append(target)
    except OSError:
        for target in created:
            target.unlink(missing_ok=True)
        return None
    return merged, inserted, replaced


def _parse_mineru_with_ocr_retry(mineru, path: Path, extract_dir: Path,
                                  *, note_sink=None, label: str = "",
                                  collection: bool = False,
                                  boundary_mode: str = BOUNDARY_AUTO,
                                  num_template: str = ""):
    """MinerU 文本层含替换符、题号断档或选项错序时，强制 OCR 重跑一次。

    U+FFFD 已经写进服务端返回的合法 UTF-8 Markdown，客户端下载后无法反解。MinerU
    v4 的 ``file.is_ocr`` 正是为乱码 PDF 准备；只在首轮确实出现替换符时重跑，正常
    文件不增加调用。白名单模式刻意不把缺号和题号覆盖率用于触发或择优，但乱码、
    选项异常和数学噪声仍照常检查。第二轮仍有乱码则交给既有告警。
    """
    def _gaps(text: str) -> list[int]:
        # 复用真正的机械切块器，而不是另写一套题号正则。只看题干区，答案区从 1
        # 重启不应被当成主卷断档；少于 8 题或不是从 1/2 起步的材料不作此推断。
        import blocksplit
        numbers = [b.number for b in blocksplit.split_blocks(
            text, num_template=num_template, boundary_mode=mode)
                   if b.zone == "stem" and b.number is not None]
        if len(numbers) < 8 or min(numbers) > 2:
            return []
        lo, hi = min(numbers), max(numbers)
        return sorted(set(range(lo, hi + 1)) - set(numbers))

    def _number_coverage(text: str) -> int:
        """统计主卷识别出的不同题号数，防止“无断档的短前缀”冒充更好结果。"""
        import blocksplit
        return len({
            b.number for b in blocksplit.split_blocks(
                text, num_template=num_template, boundary_mode=mode)
            if (b.zone == "stem" and isinstance(b.number, int)
                and 1 <= b.number <= 60)
        })

    def _choice_anomalies(text: str) -> int:
        # 只抓“已经很像选择题、但无法组成内容非空 A-D 四元组”的强信号。
        # 普通裸 A/B/C/D 不带点时 looks_like_choice_options 为 False，交给入库前
        # 规范化，不为纯格式问题多花一次 OCR；这里针对上海卷二维选项错序和标签
        # 被识成 operatorname 的形态，它们会造成内容错配，必须换识别路径复核。
        import blockpipe
        import blocksplit
        import mechfix
        import qualcheck
        broad_bad = 0
        for block in blocksplit.split_blocks(
                text, num_template=num_template, boundary_mode=mode):
            if block.zone != "stem":
                continue
            candidate = mechfix.normalize_embedded_choice_labels(block.text)
            if ((mechfix.looks_like_choice_options(candidate)
                 or (mechfix.has_choice_answer_blank(candidate)
                     and len(re.findall(r"!\[[^\]]*\]\([^)]*\)",
                                        candidate)) >= 3))
                    and not mechfix.has_complete_choice_options(candidate)):
                broad_bad += 1
        # 原来的宽判据覆盖没有分区标题的试卷；整段落实题型后的窄判据补上
        # “答题括号也被吃掉、只剩 1—3 个选项”的旧卷形态。两者可能命中同一题，
        # 择优只需要异常严重度，不需要精确并集，因此取较大值避免重复计数。
        section_bad = len(qualcheck.find_option_count_anomalies(
            blockpipe.split_and_prep(
                text, num_template=num_template, boundary_mode=mode)))
        return max(broad_bad, section_bad)

    def _image_choice_coverage(text: str) -> int:
        """无文字选项标签时，以空答题括号后的图片数衡量 OCR 重跑是否更完整。"""
        import blocksplit
        import mechfix
        counts = []
        for block in blocksplit.split_blocks(
                text, num_template=num_template, boundary_mode=mode):
            if block.zone != "stem" or not mechfix.has_choice_answer_blank(block.text):
                continue
            count = len(re.findall(r"!\[[^\]]*\]\([^)]*\)", block.text))
            if count >= 3:
                counts.append(min(count, 5))
        return sum(counts)

    def _ocr_noise_anomalies(text: str) -> int:
        """统计含成组异常数学命令的题块，供文本层与强制 OCR 结果择优。"""
        import blocksplit
        import qualcheck
        vector_artifact = re.compile(
            r"<sup(?:\s[^>]*)?>\s*#\s*</sup\s*>\s*"
            r"<sup(?:\s[^>]*)?>\s*[»→]\s*</sup\s*>", re.I)
        return sum(
            1 for block in blocksplit.split_blocks(
                text, num_template=num_template, boundary_mode=mode)
            if (block.zone == "stem"
                and (qualcheck.has_dense_ocr_math_noise(block.text)
                     or vector_artifact.search(block.text))))

    def _content_coverage(text: str) -> tuple[int, int]:
        """返回可见正文量与图片引用数，用于阻止重试结果整段缩水。"""
        image_re = re.compile(
            r"!\[[^\]]*\]\([^)]*\)|<img\b[^>]*>", re.I)
        images = len(image_re.findall(text or ""))
        without_images = image_re.sub(" ", text or "")
        without_html = re.sub(r"<[^>]+>", "", without_images)
        return len("".join(without_html.split())), images

    def _publish_extract_tree(selected: Path, target: Path) -> None:
        """只把最终采用轮次的解压树发布到固定目录，禁止两轮图片混在一起。"""
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if target.is_symlink() or not target.is_dir():
                raise ConvertError(f"OCR 中间产物路径不是普通目录：{target}")
            # batch 状态必须活到下游 raw Markdown/缓存真正落盘之后。若在这里随
            # 旧目录一起删掉，进程恰好在“下载完成”和“写缓存”之间退出，重启会
            # 再次提交整本 OCR。下载成功时 .zip.part 已由客户端清理，只需把不含
            # 凭证明文或签名 URL 的状态 JSON 带进最终附件树，后续既有工作区清理
            # 会统一回收。
            for state_path in target.glob(".mineru_task_*.json"):
                if state_path.is_file() and not state_path.is_symlink():
                    shutil.copy2(state_path, selected / state_path.name)
            shutil.rmtree(target)
        try:
            shutil.move(str(selected), str(target))
        except OSError as e:
            raise ConvertError(f"保存 OCR 中间产物失败：{e}") from e

    # 两轮 OCR 必须解压到互不相交的目录。MinerU 客户端的 extractall 不会清空
    # 旧目录；共用路径会让“保留首轮 Markdown”却读取到第二轮图片，甚至在换 token
    # 重跑时继续混入上一次残留。只有完成择优后才把选中轮次发布到固定 extract_dir。
    import tempfile

    extract_dir = Path(extract_dir)
    mode = normalize_boundary_mode(boundary_mode)
    extract_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(
        prefix=f".{extract_dir.name}.ocr-", dir=extract_dir.parent))
    first_dir = staging / "text-layer"
    first_dir.mkdir()
    selected_dir = first_dir
    try:
        raw_md, name = mineru.parse_pdf(
            path,
            extract_dir=first_dir,
            # staging 会在异常时清理，不能把可恢复状态放在那里。固定的
            # extract_dir 已由任务快照持久化，下载失败后可凭 batch_id 继续，
            # 不必重新上传并执行整本 OCR。
            resume_dir=extract_dir,
            resume_key="text-layer",
        )
        # 结构合集的各单元都会从第 1 题重开。在整本还没分组时跑
        # “全卷题号断档/选项错序”体检，会把合法的重启误当故障，甚至触发
        # 119+89 页的第二轮强制 OCR。合集只在整本阶段处理不可逆乱码，
        # 选项与题号质量等切成单元后再检查。
        check_numbering = not collection and mode != BOUNDARY_WHITELIST
        first_gaps = _gaps(raw_md) if check_numbering else []
        first_coverage = _number_coverage(raw_md) if check_numbering else 0
        first_choice_bad = 0 if collection else _choice_anomalies(raw_md)
        first_image_coverage = 0 if collection else _image_choice_coverage(raw_md)
        first_ocr_bad = 0 if collection else _ocr_noise_anomalies(raw_md)
        first_content_units, first_total_images = _content_coverage(raw_md)
        should_retry = (path.suffix.lower() == ".pdf"
                        and ("�" in raw_md or first_gaps or first_choice_bad
                             or first_ocr_bad))
        if should_retry:
            prefix = f"（{label}）" if label else ""
            reason = ("文本层含乱码" if "�" in raw_md else
                      f"文本层题号断档（缺 {','.join(map(str, first_gaps))}）"
                      if first_gaps else
                      f"文本层有 {first_choice_bad} 道选择题选项错序/标签残缺"
                      if first_choice_bad else
                      f"文本层有 {first_ocr_bad} 道题含成组异常数学命令")
            if note_sink is not None:
                note_sink(f"{prefix}{path.name} {reason}，已自动改用强制 OCR 重试")
            logger.warning("[WARN] %s%s %s，强制 OCR 重试", prefix, path.name, reason)
            retry_dir = staging / "forced-ocr"
            retry_dir.mkdir()
            retry_md, retry_name = mineru.parse_pdf(
                path,
                extract_dir=retry_dir,
                force_ocr=True,
                resume_dir=extract_dir,
                resume_key="forced-ocr",
            )
            collection_merge = None
            if collection:
                # 合集的题号会在每个专题重新从 1 开始，不能用整本覆盖率比较。
                # 两轮都已实际完成时，以强制 OCR 为骨架，只吸收文本层里位置可
                # 唯一证明的缺题/更完整同号题；结构有歧义则原样回落下方整本择优。
                collection_merge = _merge_collection_ocr_variants(
                    retry_md, retry_dir, raw_md, first_dir)
                if collection_merge is not None:
                    retry_md, merged_missing, merged_better = collection_merge
                    if note_sink is not None and (merged_missing or merged_better):
                        details = []
                        if merged_missing:
                            details.append(f"补回 {merged_missing} 道缺题")
                        if merged_better:
                            details.append(f"替换 {merged_better} 道更完整同号题")
                        note_sink(
                            f"{prefix}{path.name} 已按合集单元与题号机械择优："
                            + "，".join(details))
                else:
                    logger.info(
                        "%s%s 两轮合集结构不能唯一对应，回落整本择优",
                        prefix, path.name)
            retry_gaps = _gaps(retry_md) if check_numbering else []
            retry_coverage = _number_coverage(retry_md) if check_numbering else 0
            retry_choice_bad = 0 if collection else _choice_anomalies(retry_md)
            retry_image_coverage = 0 if collection else _image_choice_coverage(retry_md)
            retry_ocr_bad = 0 if collection else _ocr_noise_anomalies(retry_md)
            retry_content_units, retry_total_images = _content_coverage(retry_md)
            # 不能只比较“断档数”：OCR 若只识别出连续的 1—9 题，断档数反而是 0，
            # 会把文本层已识别出的 21 题整体覆盖掉。题号覆盖收益先扣除断档惩罚，
            # 再比选项、数学噪声和图片覆盖。
            first_number_quality = first_coverage - 2 * len(first_gaps)
            retry_number_quality = retry_coverage - 2 * len(retry_gaps)
            first_score = (-first_number_quality, -first_coverage,
                           first_choice_bad, first_ocr_bad,
                           -first_image_coverage)
            retry_score = (-retry_number_quality, -retry_coverage,
                           retry_choice_bad, retry_ocr_bad,
                           -retry_image_coverage)

            # 题号相同并不等于正文完整。强制 OCR 比文本层少超过 25% 且至少 120
            # 个可见单位，或少了任意图片时，一律保留首轮并要求人工校对；这比让
            # 题号完整但题干缺段的版本免审入库更可恢复。
            text_shrunk = (
                first_content_units - retry_content_units >= 120
                and retry_content_units * 4 < first_content_units * 3
            )
            images_shrunk = retry_total_images < first_total_images
            coverage_shrunk = text_shrunk or images_shrunk
            use_retry = retry_score < first_score and not coverage_shrunk
            if ("�" in raw_md and retry_coverage >= first_coverage
                    and not coverage_shrunk):
                use_retry = True
            if coverage_shrunk and note_sink is not None:
                details = []
                if text_shrunk:
                    details.append(
                        f"可见正文由约 {first_content_units} 缩至 {retry_content_units}")
                if images_shrunk:
                    details.append(
                        f"图片由 {first_total_images} 张减至 {retry_total_images} 张")
                note_sink(qualcheck.mark_manual_review(
                    f"{prefix}{path.name} 强制 OCR 结果显著缩水（{'，'.join(details)}），"
                    "已保留文本层结果；请对照原文件校对"))
            if use_retry:
                raw_md, name = retry_md, retry_name
                selected_dir = retry_dir
            elif note_sink is not None and not coverage_shrunk:
                if check_numbering:
                    note_sink(
                        f"{prefix}{path.name} 强制 OCR 仅识别出 {retry_coverage} 个题号，"
                        f"少于/劣于文本层的 {first_coverage} 个，已保留文本层结果")
                else:
                    note_sink(
                        f"{prefix}{path.name} 强制 OCR 的选项/数学噪声质量未优于文本层，"
                        "已保留文本层结果")
            # 质量门只看最终实际采用的那一版。重试更差而被弃用时不能按 retry_*
            # 误报警；保留的文本层仍异常时也不能被“重试无异常”掩盖。
            final_gaps = retry_gaps if use_retry else first_gaps
            final_choice_bad = retry_choice_bad if use_retry else first_choice_bad
            final_ocr_bad = retry_ocr_bad if use_retry else first_ocr_bad
            final_source = "强制 OCR 后" if use_retry else "保留的文本层仍"
            if final_gaps and note_sink is not None:
                note_sink(qualcheck.mark_manual_review(
                    f"{prefix}{path.name} {final_source}缺题号 "
                    f"{','.join(map(str, final_gaps))}，请人工补题后再入库"))
            if final_choice_bad and note_sink is not None:
                note_sink(qualcheck.mark_manual_review(
                    f"{prefix}{path.name} {final_source}有 {final_choice_bad} 道"
                    "选择题选项错序/标签残缺，请对照原卷校对"))
            if final_ocr_bad and note_sink is not None:
                note_sink(qualcheck.mark_manual_review(
                    f"{prefix}{path.name} {final_source}有 {final_ocr_bad} 道题"
                    "含成组异常数学命令，请对照原卷校对"))
        _publish_extract_tree(selected_dir, extract_dir)
        return raw_md, name
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _parse_with_ocr_backend(path: Path, extract_dir: Path, cfg, *,
                            ocr_backend: str, doc2x_api_key: str = "",
                            note_sink=None, label: str = "",
                            collection: bool = False,
                            boundary_mode: str = BOUNDARY_AUTO,
                            num_template: str = "",
                            image_page_count: int = 0):
    """统一 OCR 入口，返回 ``(原文, 文件名, 归因信息)``。

    MinerU 的强制 OCR 重试与坐标图片修复只属于 MinerU；Doc2X 自己已经给出排版
    后的 Markdown，不能再套 MinerU 的 content_list 修复，否则会拿不存在的坐标账
    误改图片。两条链路在这里汇合后，共用判空、选项检查与下游拆题。
    """
    backend = normalize_ocr_backend(ocr_backend)
    if backend == OCR_DOC2X:
        # 重转同一上传件会复用 stem。Doc2X 解包同样不能直接覆盖固定目录：上轮
        # 遗留的同名图可能被本轮未改写的 Markdown 引用，造成文字和图片跨轮串台。
        import tempfile

        final_dir = Path(extract_dir)
        final_dir.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(
            prefix=f".{final_dir.name}.doc2x-", dir=final_dir.parent))
        try:
            result = ocr_pool.run(
                "doc2x",
                lambda api_key: doc2x_client.Doc2XClient(api_key).parse_pdf(
                    path, extract_dir=staging),
                fallback=doc2x_api_key,
            )
        except doc2x_client.Doc2XError as exc:
            raise ConvertError(str(exc)) from exc
        except Exception:
            raise
        else:
            try:
                if final_dir.exists():
                    if final_dir.is_symlink() or not final_dir.is_dir():
                        raise ConvertError(
                            f"OCR 中间产物路径不是普通目录：{final_dir}")
                    shutil.rmtree(final_dir)
                shutil.move(str(staging), str(final_dir))
            except ConvertError:
                raise
            except OSError as exc:
                raise ConvertError(f"保存 Doc2X 中间产物失败：{exc}") from exc
            low_pages = [i + 1 for i, score in enumerate(result.page_scores)
                         if isinstance(score, int) and score < 80]
            if low_pages and note_sink is not None:
                prefix = f"（{label}）" if label else ""
                note_sink(qualcheck.mark_manual_review(
                    f"{prefix}Doc2X 第 {','.join(map(str, low_pages))} 页质量分低于 80，"
                    "请在校对页重点核对"))
            meta = {
                "doc2x_model": result.model,
                "doc2x_uid": result.uid,
                "doc2x_page_scores": list(result.page_scores),
            }
            markdown, page_meta = _apply_image_page_boundaries(
                result.markdown, final_dir,
                boundary_mode=boundary_mode,
                image_page_count=image_page_count,
                ocr_backend=backend, note_sink=note_sink, label=label)
            meta.update(page_meta)
            return markdown, result.markdown_name, meta
        finally:
            # 成功时 staging 已被 move，失败时清掉未完成解包；固定目录只会包含
            # 一次完整结果，不会暴露半轮产物。
            shutil.rmtree(staging, ignore_errors=True)

    from src.mineru_client import MineruClient
    raw_md, name = ocr_pool.run(
        OCR_MINERU,
        lambda token: _parse_mineru_with_ocr_retry(
            MineruClient(token, cfg.mineru_model_version), path, extract_dir,
            note_sink=note_sink, label=label, collection=collection,
            boundary_mode=boundary_mode, num_template=num_template),
        fallback=cfg.mineru_token)
    raw_md, page_meta = _apply_image_page_boundaries(
        raw_md, extract_dir, boundary_mode=boundary_mode,
        image_page_count=image_page_count, ocr_backend=backend,
        note_sink=note_sink, label=label)
    raw_md = _repair_choice_images(
        raw_md, extract_dir, note_sink, label=label)
    return raw_md, name, page_meta


def _ensure_raw_text(raw_md: str, path: Path, label: str = "", *,
                     ocr_backend: str = OCR_MINERU) -> None:
    """OCR 原文一个字都没有时立刻失败，不要接着往 LLM 走。

    这不是理论情况：pdfcpu 之类工具「压缩」出来的坏 PDF，整页是一张纯黑位图、
    内嵌字体流又是坏的 zlib，MinerU 只会返回两行 `![](images/xxx.jpg)`，没有任何
    文字。继续往下走，切块切出 0 块、规范化返回空串，最后是一个「转换成功但没有
    内容」的组——看板显示已就绪、点进去却说没转完（那正是这个检查要根除的症状）。
    在这里就断掉，用户看到的是原因而不是矛盾的状态。
    """
    if _has_text_beyond_images(raw_md):
        return
    prefix = f"（{label}）" if label else ""
    service = "Doc2X" if normalize_ocr_backend(ocr_backend) == OCR_DOC2X else "MinerU"
    raise ConvertError(
        f"{prefix}{service} 没有从 {path.name} 里识别出任何文字，只有整页图像。"
        "这份 PDF 大概率是坏的（页面渲染为纯黑/空白，或内嵌字体已损坏），"
        "请换一份能正常打开、能选中文字的原始文件重新上传")


def _ensure_normalized(md: str, path: Path) -> str:
    """规范化结果为空时按失败处理，绝不当成「转换完成」返回。

    原文有字但一道题也没切出来，只会是切块/规范化这一层的问题（题号写法不认、
    模板指定错了、整份其实不是题目文档）。返回空串给上层会变成一个点不开的
    「已就绪」组，所以这里改成显式失败，并把下一步该做什么写进消息里。
    """
    if md.strip():
        return md
    raise ConvertError(
        f"已识别出 {path.name} 的原文，但没能从中切出任何题目。"
        "可能是题号写法没被认出（可在「重新转换」里指定题号模板），"
        "或这份文件其实不含题目")


def _corpus_meta(cfg, engine: str, num_template: str = "", *,
                 ocr_backend: str = OCR_MINERU, ocr_meta=None,
                 boundary_mode: str = BOUNDARY_AUTO) -> dict:
    """语料留档的归因信息（见 corpus.archive 的 meta 参数）。

    `mineru_model_version` 是这里最有价值的一项：MinerU 在 2026-08 把 `vlm` 从
    3.4.0 静默升到 3.4.4 并开始整项丢掉行内公式选项（optcheck 模块头记着这件
    事），当时只能人肉比对两批卷子才定位到版本。留下这个字段，同类事故按版本
    分组即可看出来。cfg 的形状由 project-alpha 决定，取不到就留空——归因缺一项
    不该让留档整体失败。
    """
    payload = {
        "engine": engine,
        "num_template": num_template or "",
        "boundary_mode": normalize_boundary_mode(boundary_mode),
        "ocr_backend": normalize_ocr_backend(ocr_backend),
    }
    if payload["ocr_backend"] == OCR_MINERU:
        payload["mineru_model_version"] = getattr(
            cfg, "mineru_model_version", None)
    payload.update(ocr_meta or {})
    return payload


def _load_llm_fallback_cfg():
    """finish_block_review 的 action=ai 分支在 provider 为 None 时需要的
    project-alpha 集中 DeepSeek 配置（老回落行为）。这一步不需要 MinerU
    token——只读 cfg.deepseek_api_key/model，不像 _load_config_for_user 那样
    要拿用户 token 去替换字段，所以单独给一个不要求 token 的入口。
    """
    from src.config import load_config

    # 与 _load_config_for_user 同一个理由：两个键都得补，只补 MinerU 那个的话
    # 照样会因为 DEEPSEEK_API_KEY 为空抛 ConfigError。
    for key in ("MINERU_API_TOKEN", "DEEPSEEK_API_KEY"):
        if not os.environ.get(key, "").strip():
            os.environ[key] = _ENV_PLACEHOLDER
    return load_config()


def _remove_ocr_workspace(path: Path, *, unit_only: bool = False) -> None:
    """只删除 project-alpha ``output/raw_md`` 下的明确工作区。"""
    target = Path(path)
    try:
        root = _RAW_MD_ROOT.resolve()
        resolved = target.resolve()
    except OSError:
        return
    if root not in resolved.parents:
        logger.warning("[WARN] 拒绝清理 OCR 根目录外的路径: %s", target)
        return
    if unit_only and not resolved.name.startswith("collection_unit_"):
        logger.warning("[WARN] 拒绝清理非合集单元工作区: %s", target)
        return
    if target.is_symlink():
        logger.warning("[WARN] 拒绝递归清理符号链接工作区: %s", target)
        return
    shutil.rmtree(target, ignore_errors=True)


def cleanup_collection_workspace(path: str | Path) -> None:
    """合集批次终态后回收一个单元的 OCR 后工作区。"""
    _remove_ocr_workspace(Path(path), unit_only=True)


def collection_workspace_root() -> Path:
    """供启动清理器只读扫描合集临时工作区根目录。"""
    return _RAW_MD_ROOT


def _copy_collection_images(markdown: str, source_dir: Path,
                            target_dir: Path) -> None:
    """只复制当前单元实际引用的图片，使各子任务的生命周期独立。"""
    refs = _image_ref_counter(markdown)
    if not refs:
        return
    source_images = Path(source_dir) / "images"
    target_images = Path(target_dir) / "images"
    for raw_name in refs:
        safe_name = Path(raw_name).name
        source = source_images / safe_name
        if not source.is_file():
            # 不在这里伪造空图。单元收尾的 _intercept_images 会把断图
            # 变成强制校对提示，且保留原引用供人抢救。
            continue
        target_images.mkdir(parents=True, exist_ok=True)
        target = target_images / safe_name
        if not target.exists():
            shutil.copy2(source, target)


_COLLECTION_CACHE_MD = "_collection_raw.md"
_COLLECTION_CACHE_META = "_collection_meta.json"


def _collection_cache_workspaces(has_solution: bool, supplied=None
                                 ) -> list[Path]:
    """取得本次整本识别的可恢复工作区，并严格限制在 raw_md 根下。"""
    count = 2 if has_solution else 1
    if supplied:
        workspaces = [Path(path).resolve() for path in supplied]
        if len(workspaces) != count:
            raise ConvertError("合集 OCR 缓存目录数量与题干/解析文件不一致")
    else:
        token = uuid.uuid4().hex
        workspaces = [
            _raw_md_dir(f"collection_unit_cache_{token}_exam").resolve()
        ]
        if has_solution:
            workspaces.append(
                _raw_md_dir(f"collection_unit_cache_{token}_solution").resolve())

    root = _RAW_MD_ROOT.resolve()
    for workspace in workspaces:
        try:
            workspace.relative_to(root)
        except ValueError as exc:
            raise ConvertError("合集 OCR 缓存目录越过临时工作区边界") from exc
        if (workspace.parent != root
                or not workspace.name.startswith("collection_unit_cache_")):
            raise ConvertError("合集 OCR 缓存目录名称或层级不合法")
        if workspace.is_symlink():
            raise ConvertError("合集 OCR 缓存目录不能是符号链接")
    return workspaces


def allocate_collection_cache_dirs(has_solution: bool) -> list[str]:
    """在发起外部 OCR 前分配缓存路径，供上层先持久化再调用。"""
    return [str(path) for path in _collection_cache_workspaces(
        bool(has_solution))]


def _read_collection_cache(workspace: Path):
    markdown_path = workspace / _COLLECTION_CACHE_MD
    meta_path = workspace / _COLLECTION_CACHE_META
    if not markdown_path.is_file() or not meta_path.is_file():
        return None
    try:
        markdown = markdown_path.read_text(encoding="utf-8")
        payload = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not markdown.strip() or not isinstance(payload, dict):
        return None
    meta = payload.get("ocr_meta")
    return markdown, (meta if isinstance(meta, dict) else {})


def _write_collection_cache(workspace: Path, markdown: str, ocr_meta) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    if workspace.is_symlink():
        raise ConvertError("合集 OCR 缓存目录不能是符号链接")
    marker = uuid.uuid4().hex
    markdown_tmp = workspace / f".{_COLLECTION_CACHE_MD}.{marker}.tmp"
    meta_tmp = workspace / f".{_COLLECTION_CACHE_META}.{marker}.tmp"
    try:
        markdown_tmp.write_text(markdown, encoding="utf-8")
        meta_tmp.write_text(json.dumps(
            {"version": 1, "ocr_meta": ocr_meta or {}},
            ensure_ascii=False), encoding="utf-8")
        os.replace(markdown_tmp, workspace / _COLLECTION_CACHE_MD)
        os.replace(meta_tmp, workspace / _COLLECTION_CACHE_META)
    finally:
        markdown_tmp.unlink(missing_ok=True)
        meta_tmp.unlink(missing_ok=True)


def collection_cache_snapshot(cache_dirs, *, has_solution: bool) -> dict:
    """读取可人工修订的合集 OCR 原文，不向路由层暴露缓存路径。"""
    workspaces = _collection_cache_workspaces(bool(has_solution), cache_dirs)
    documents = []
    digest = hashlib.sha256()
    for index, workspace in enumerate(workspaces):
        cached = _read_collection_cache(workspace)
        if cached is None:
            side = "解析" if index else "题干"
            raise ConvertError(f"{side} OCR 原文缓存不存在或已损坏")
        markdown, ocr_meta = cached
        encoded = markdown.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        documents.append({
            "side": "solution" if index else "exam",
            "markdown": markdown,
            "ocr_meta": ocr_meta,
        })
    return {
        "exam_markdown": documents[0]["markdown"],
        "solution_markdown": (
            documents[1]["markdown"] if len(documents) > 1 else ""),
        "ocr_meta": {
            "exam": documents[0]["ocr_meta"],
            "solution": (documents[1]["ocr_meta"]
                         if len(documents) > 1 else None),
        },
        "revision": digest.hexdigest(),
    }


def collection_cache_is_editable(cache_dirs, *, has_solution: bool) -> bool:
    """失败看板只在完整 OCR 原文仍可读取时展示人工调整入口。"""
    try:
        collection_cache_snapshot(cache_dirs, has_solution=has_solution)
    except ConvertError:
        return False
    return True


def update_collection_cache_markdown(cache_dirs, *, has_solution: bool,
                                     exam_markdown: str,
                                     solution_markdown: str = "",
                                     expected_revision: str = "") -> dict:
    """原子替换人工修改后的 OCR Markdown，并保留原始 OCR 元数据。"""
    snapshot = collection_cache_snapshot(
        cache_dirs, has_solution=has_solution)
    if (expected_revision
            and snapshot["revision"] != str(expected_revision).strip()):
        raise ConvertError("识别原文已在其他窗口发生变化，请刷新后重新修改")

    documents = [str(exam_markdown or "")]
    if has_solution:
        documents.append(str(solution_markdown or ""))
    for index, markdown in enumerate(documents):
        side = "解析" if index else "题干"
        if not markdown.strip():
            raise ConvertError(f"{side} Markdown 不能为空")
        if len(markdown.encode("utf-8")) > config.MAX_MD_FILE_BYTES:
            limit = config.MAX_MD_FILE_BYTES // (1024 * 1024)
            raise ConvertError(f"{side} Markdown 超过 {limit}MB 上限")
        if not _has_text_beyond_images(markdown):
            raise ConvertError(f"{side} Markdown 没有可识别的文字")

    workspaces = _collection_cache_workspaces(bool(has_solution), cache_dirs)
    metadata = [snapshot["ocr_meta"]["exam"]]
    if has_solution:
        metadata.append(snapshot["ocr_meta"]["solution"])
    for workspace, markdown, ocr_meta in zip(
            workspaces, documents, metadata, strict=True):
        _write_collection_cache(workspace, markdown, ocr_meta)
    return collection_cache_snapshot(cache_dirs, has_solution=has_solution)


def materialize_collection_cache_as_unit(cache_dirs, *, has_solution: bool,
                                         title: str,
                                         ocr_backend: str) -> dict:
    """把人工确认的整本 OCR 原文作为单组落盘，后续只跑机械题号拆分。"""
    workspaces = _collection_cache_workspaces(bool(has_solution), cache_dirs)
    snapshot = collection_cache_snapshot(
        cache_dirs, has_solution=has_solution)
    combined = snapshot["exam_markdown"].rstrip()
    if has_solution:
        combined += ("\n\n# 参考答案与解析\n\n"
                     + snapshot["solution_markdown"].lstrip())

    scope = f"collection_unit_{uuid.uuid4().hex}"
    unit_dir = _raw_md_dir(scope)
    unit_dir.mkdir(parents=True, exist_ok=False)
    try:
        # 双文件首次识别时已把解析图片按命名空间复制到题干缓存，因此这里
        # 统一从首个缓存复制即可，且只复制人工 Markdown 仍然引用的图片。
        _copy_collection_images(combined, workspaces[0], unit_dir)
        raw_path = unit_dir / f"{scope}_raw.md"
        raw_path.write_text(combined, encoding="utf-8")
    except Exception:
        _remove_ocr_workspace(unit_dir, unit_only=True)
        raise
    return {
        "title": str(title or "人工调整结果"),
        "raw_path": str(raw_path),
        "workspace_dir": str(unit_dir),
        "scope": scope,
        "include_solution": bool(has_solution),
        "ocr_backend": normalize_ocr_backend(ocr_backend),
        "ocr_meta": snapshot["ocr_meta"],
        "collection_cache_dirs": [str(path) for path in workspaces],
    }


_COLLECTION_RECOVERY_VERSION = 1
_COLLECTION_RECOVERY_ROOT = "_collection_recovery"
_COLLECTION_RECOVERY_MAX_CROPS = 32


def _restore_collection_image_namespace(markdown: str, workspace: Path,
                                        side: str) -> str:
    """把上次双文件合并写回缓存的 ``exam_`` / ``solution_`` 图名还原。

    content_list 中仍是 MinerU 原图名；只有还原后才能幂等重跑最新的
    坐标选项修复。仅在原名与带前缀文件都存在且内容摘要一致时还原，
    因此不会把用户原本就叫 ``exam_xxx`` 的图片误改名。
    """
    images = Path(workspace) / "images"
    prefix = f"{side}_"
    digest_cache: dict[Path, bytes] = {}

    def _digest(path: Path) -> bytes:
        cached = digest_cache.get(path)
        if cached is None:
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            cached = digest.digest()
            digest_cache[path] = cached
        return cached

    def _original(raw_name: str) -> str | None:
        safe_name = Path(raw_name).name
        if safe_name != raw_name or not safe_name.startswith(prefix):
            return None
        original_name = safe_name[len(prefix):]
        prefixed = images / safe_name
        original = images / original_name
        try:
            if (not prefixed.is_file() or prefixed.is_symlink()
                    or not original.is_file() or original.is_symlink()
                    or _digest(prefixed) != _digest(original)):
                return None
        except OSError:
            return None
        return original_name

    def _replace_md(match: "re.Match") -> str:
        alt, raw_name = match.groups()
        original = _original(raw_name)
        return (f"![{alt}](images/{original})" if original is not None
                else match.group(0))

    value = _IMG_REF_RE.sub(_replace_md, markdown or "")

    def _replace_html(match: "re.Match") -> str:
        original = _original(match.group(1))
        if original is None:
            return match.group(0)
        whole = match.group(0)
        start, end = match.span(1)
        offset = match.start()
        return whole[:start - offset] + original + whole[end - offset:]

    return _HTML_IMG_REF_RE.sub(_replace_html, value)


def _collection_model_json(workspace: Path, source_pdf: Path, *,
                           reference_markdown: str = "") -> Path:
    """从 MinerU 多类布局 JSON 中选出与源 PDF、原文最吻合的一份。"""
    from pypdf import PdfReader

    workspace = Path(workspace).resolve()
    try:
        page_count = len(PdfReader(str(source_pdf)).pages)
    except Exception as exc:
        raise ConvertError(f"无法读取合集源 PDF 页数：{exc}") from exc
    candidates: dict[Path, int] = {}
    for pattern, rank in (("*model.json", 1),
                          ("*_content_list.json", 2),
                          ("*_content_list_v2.json", 3),
                          ("layout.json", 4)):
        for candidate in workspace.rglob(pattern):
            try:
                candidates[candidate.resolve()] = rank
            except OSError:
                continue

    reference_blocks = collection_recovery._markdown_question_blocks(
        reference_markdown) if reference_markdown else {}
    reference_count = sum(len(items) for items in reference_blocks.values())
    matches: list[tuple[tuple[int, ...], Path]] = []
    for resolved, rank in candidates.items():
        if _COLLECTION_RECOVERY_ROOT in resolved.parts:
            continue
        try:
            resolved.relative_to(workspace)
            if resolved.is_symlink() or not resolved.is_file():
                continue
            document = collection_recovery.load_layout_document(resolved)
        except (OSError, ValueError, collection_recovery.CollectionRecoveryError):
            continue
        if document.page_count != page_count:
            continue
        if reference_markdown:
            unique_hits, ambiguous_hits = (
                collection_recovery.layout_reference_score(
                    document, reference_markdown))
            if reference_count >= 2 and unique_hits < 2:
                continue
            score = (unique_hits, -ambiguous_hits, len(document.questions),
                     rank, len(document.lines))
        else:
            if not document.questions:
                continue
            score = (len(document.questions), len(document.units), rank,
                     len(document.lines))
        matches.append((score, resolved))
    if not matches:
        raise ConvertError(
            "合集局部恢复没有找到与源 PDF 等页、且正文锚点足够的 MinerU 布局 JSON")
    matches.sort(key=lambda item: item[0], reverse=True)
    best_score = matches[0][0]
    best = [path for score, path in matches if score == best_score]
    if len(best) != 1:
        raise ConvertError(
            f"合集局部恢复有 {len(best)} 份同分 MinerU 布局 JSON，拒绝任意选择")
    return best[0]


def _collection_unit_blocks(unit, *, allow_out_of_order: bool = False):
    blocks = _collection_blocks(unit, allow_out_of_order=allow_out_of_order)
    if blocks is None:
        raise ConvertError(
            f"合集单元“{unit.title}”的题号不唯一或阅读顺序不能确认，"
            "已停止自动局部恢复")
    return blocks


def _collection_number_gaps(unit, *, allow_out_of_order: bool = False
                            ) -> list[int]:
    numbers = [
        block.number for block in _collection_unit_blocks(
            unit, allow_out_of_order=allow_out_of_order)
    ]
    if not numbers or numbers[0] != 1:
        raise ConvertError(
            f"合集单元“{unit.title}”没有可靠的第 1 题，"
            "无法为局部 OCR 建立前锚点")
    return sorted(set(range(1, max(numbers) + 1)) - set(numbers))


def _weak_collection_question_numbers(unit, *, allow_out_of_order: bool = False
                                      ) -> list[int]:
    """只标记两种可证明的弱题：选项完整但题干几乎无中文，或多图选择题仍无法形成 A—D。"""
    import mechfix

    weak: list[int] = []
    option_head = re.compile(
        r"(?m)^\s*(?:\$\s*\\?displaystyle\s*)?[（(]?[AＡ]\s*[.\uff0e、)）]")
    for block in _collection_unit_blocks(
            unit, allow_out_of_order=allow_out_of_order):
        normalized = mechfix.normalize_embedded_choice_labels(block.text)
        complete = mechfix.has_complete_choice_options(normalized)
        match = option_head.search(normalized)
        stem = normalized[:match.start()] if match else normalized
        stem = _HTML_IMG_REF_RE.sub(" ", _IMG_REF_RE.sub(" ", stem))
        chinese = len(re.findall(r"[\u3400-\u9fff]", stem))
        images = _collection_block_image_count(normalized)
        text_shell = complete and chinese < 3
        ambiguous_images = (
            mechfix.has_choice_answer_blank(normalized)
            and images >= 4 and not complete
        )
        if text_shell or ambiguous_images:
            weak.append(block.number)
    return weak


def _mechanical_missing_solution_numbers(exam_unit, solution_unit):
    """以真正入库前使用的机械切块/配对口径计算缺解析。

    解析原文有时没有显式题号，但 ``blocksplit`` 能根据连续
    ``【详解】`` 补回号码。直接比较 Markdown 行首题号会把这类已经能安全
    配对的解析误判为缺失，甚至在末卷虚构一个不存在的裁片边界。
    """
    import blockpipe
    import blocksplit

    combined = (exam_unit.markdown.rstrip()
                + "\n\n# 参考答案与解析\n\n"
                + solution_unit.markdown.lstrip())
    pairing = blocksplit.pair_blocks(blockpipe.split_and_prep(combined))
    missing = sorted({
        stem.number for stem, answer in pairing.paired
        if answer is None and isinstance(stem.number, int)
    })
    return missing, pairing


def _replace_collection_unit(document: str, original_unit: str,
                             rebuilt_unit: str) -> str:
    start = document.find(original_unit)
    if start < 0 or document.find(original_unit, start + 1) >= 0:
        raise ConvertError("合集单元在整本 Markdown 中不唯一，拒绝替换")
    return document[:start] + rebuilt_unit + document[start + len(original_unit):]


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _collection_recovery_key(source_sha256: str,
                             plan: collection_recovery.GapCropPlan) -> str:
    payload = {
        "version": _COLLECTION_RECOVERY_VERSION,
        "source_sha256": source_sha256,
        "title": plan.unit_title,
        "numbers": list(plan.missing_numbers),
        "previous": plan.previous_number,
        "next": plan.next_number,
        "slices": [dataclasses.asdict(part) for part in plan.slices],
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:24]


def _attach_proven_leading_solution_images(
        local_markdown: str, selected: dict[int, str], local_dir: Path, *,
        missing_numbers: tuple[int, ...], content_role: str,
        ) -> dict[int, str]:
    """把阅读顺序跑到首题前、但坐标可证明属于缺题的图片归还给缺题。

    MinerU 的两栏/图文环绕页会先输出右侧图片，再输出左侧题号。不能按 Markdown
    顺序把这种图塞给前锚点。本函数只接受单缺解析、多问 ``(1)/(2)`` 结构：图片
    必须是首题前唯一内容，坐标位于含 ``目标题号.(1)`` 的融合文本块之后，并与
    该题唯一 ``(2)`` 文本块相交。证据少一项就显式失败，不猜图片归属。
    """
    lines = (local_markdown or "").splitlines(keepends=True)
    offset = 0
    first_head = None
    for line in lines:
        if isinstance(collection_structure._question_number(line), int):
            first_head = offset
            break
        offset += len(line)
    prefix = local_markdown[:first_head] if first_head is not None else ""
    prefix_refs = _image_references(prefix)
    if not prefix_refs:
        return selected
    if content_role != "solution" or len(missing_numbers) != 1:
        raise ConvertError("局部裁片题号前出现图片，但当前结构不足以证明图片归属")
    if len(prefix_refs) != 1:
        raise ConvertError("局部裁片题号前出现多张图片，拒绝猜测归属")
    remainder = prefix
    for start, end, _, _ in reversed(prefix_refs):
        remainder = remainder[:start] + remainder[end:]
    if re.sub(r"<[^>]+>|[\s\u3000]", "", remainder):
        raise ConvertError("局部裁片首题前除图片外还有正文，拒绝移动图片")

    target = missing_numbers[0]
    target_text = selected.get(target)
    if not target_text:
        raise ConvertError(f"局部裁片尚未选出第 {target} 题，不能归还图片")
    ref_path, ref_whole = prefix_refs[0][2], prefix_refs[0][3]
    if (Path(ref_path).name != ref_path
            or any(mark in ref_path for mark in ("/", "\\", "%"))):
        raise ConvertError("局部裁片前置图片路径不安全")
    if sum(text.count(ref_whole) for text in selected.values() if text) != 0:
        raise ConvertError("局部裁片前置图片已经出现在题块中，拒绝重复归还")

    layout = imgorder.load_layout(local_dir)
    box = imgorder._layout_box(ref_path, layout) if layout is not None else None
    model_paths = [
        path for path in Path(local_dir).glob("*_model.json")
        if path.is_file() and not path.is_symlink()
    ]
    if box is None or len(model_paths) != 1:
        raise ConvertError("局部裁片前置图片缺少唯一 MinerU 坐标证据")
    try:
        _, layout_lines = collection_recovery._read_layout_lines(model_paths[0])
    except collection_recovery.CollectionRecoveryError as exc:
        raise ConvertError(f"无法读取局部裁片图片坐标证据：{exc}") from exc

    target_head = re.compile(
        rf"(?<!\d){target}[ \t]*[.．、][ \t]*[（(][ \t]*1[ \t]*[)）]")
    fused = [line for line in layout_lines if target_head.search(line.text)]
    target_key = collection_recovery._key(target_text)
    part2 = []
    for line in layout_lines:
        if not re.match(r"^[ \t]*[（(][ \t]*2[ \t]*[)）]", line.text):
            continue
        line_key = collection_recovery._key(line.text)
        if len(line_key) >= 12 and line_key in target_key:
            part2.append(line)
    if len(fused) != 1 or len(part2) != 1:
        raise ConvertError(
            f"第 {target} 题前置图片缺少唯一的 (1)/(2) 版面锚点")
    fused_line, part2_line = fused[0], part2[0]
    try:
        image_top, image_bottom = float(box.bbox[1]), float(box.bbox[3])
        fused_bottom = float(fused_line.bbox[3]) * 1000
        part2_top = float(part2_line.bbox[1]) * 1000
        part2_bottom = float(part2_line.bbox[3]) * 1000
    except (AttributeError, TypeError, ValueError, IndexError) as exc:
        raise ConvertError("局部裁片前置图片坐标格式无效") from exc
    if not (
            int(box.page) == fused_line.page_index == part2_line.page_index
            and fused_bottom <= image_top < part2_bottom
            and image_bottom > part2_top):
        raise ConvertError(
            f"第 {target} 题前置图片没有同时位于 (1) 之后并与 (2) 相交")

    target_lines = target_text.splitlines()
    part2_indexes = [
        index for index, line in enumerate(target_lines)
        if re.match(r"^[ \t]*[（(][ \t]*2[ \t]*[)）]", line)
    ]
    if len(part2_indexes) != 1:
        raise ConvertError(f"第 {target} 题正文中的 (2) 标记不是唯一的")
    insert_at = part2_indexes[0] + 1
    target_lines[insert_at:insert_at] = ["", ref_whole, ""]
    updated = dict(selected)
    updated[target] = "\n".join(target_lines).strip()
    if sum(text.count(ref_whole) for text in updated.values() if text) != 1:
        raise ConvertError("局部裁片前置图片归还后引用数量不守恒")
    return updated


def _choice_verdict_hits(text: str) -> dict[str, bool] | None:
    """提取 ``故 A 正确`` 一类判定；同一选项重复时返回 ``None``。"""
    hits = re.findall(
        r"故[ \t]*([A-DＡ-Ｄ])[ \t]*(?:选项[ \t]*)?"
        r"(正确|错误|不正确|错)(?![误])",
        text or "",
    )
    verdicts: dict[str, bool] = {}
    for raw_label, verdict in hits:
        label = raw_label.translate(str.maketrans("ＡＢＣＤ", "ABCD"))
        if label in verdicts:
            return None
        verdicts[label] = verdict == "正确"
    return verdicts


def _choice_verdicts(text: str) -> dict[str, bool] | None:
    """提取 ``故 A 正确；…故 D 错误`` 的完整四选项判定。"""
    verdicts = _choice_verdict_hits(text)
    return verdicts if verdicts is not None and set(verdicts) == set("ABCD") else None


def _recover_unheaded_choice_solution(
        local_markdown: str, local_dir: Path, *, previous_number: int,
        missing_number: int) -> tuple[str, str]:
    """用完整 A—D 判定和 MinerU 布局块恢复无题号选择题解析。"""
    if missing_number != previous_number + 1:
        raise ConvertError("无题号选择题恢复只支持紧邻前锚点的下一题")
    blocks = collection_recovery._markdown_question_blocks(local_markdown)
    anchors = blocks.get(previous_number, [])
    if len(anchors) != 1 or blocks.get(missing_number):
        raise ConvertError(
            "无题号选择题恢复缺少唯一前锚点或目标并非无题号："
            f"前锚 {len(anchors)} 个，目标 {len(blocks.get(missing_number, []))} 个")
    anchor = anchors[0]
    model_paths = [
        path for path in Path(local_dir).glob("*_model.json")
        if path.is_file() and not path.is_symlink()
    ]
    if len(model_paths) != 1:
        raise ConvertError("无题号选择题恢复缺少唯一 MinerU model.json")
    try:
        _, layout_lines = collection_recovery._read_layout_lines(model_paths[0])
    except collection_recovery.CollectionRecoveryError as exc:
        raise ConvertError(f"无法读取无题号选择题布局：{exc}") from exc
    verdict_candidates: list[tuple[object, dict[str, bool]]] = []
    for index, line in enumerate(layout_lines):
        first_hits = _choice_verdict_hits(line.text)
        # 一道完整选择题解析可以跨页，但起始块必须至少明确判定 A、B，不能从
        # 后半段的单个“故 D 正确”倒推题首。
        if first_hits is None or not {"A", "B"}.issubset(first_hits):
            continue
        tail_verdicts = _choice_verdicts(
            "\n".join(item.text for item in layout_lines[index:]))
        if tail_verdicts is not None:
            verdict_candidates.append((line, tail_verdicts))
    previous_lines = [
        line for line in layout_lines
        if collection_structure._question_number(line.text) == previous_number
    ]
    if len(verdict_candidates) != 1 or len(previous_lines) != 1:
        raise ConvertError("无题号选择题缺少唯一四选项判定块或前锚布局")
    (verdict_line, layout_verdicts), previous_line = (
        verdict_candidates[0], previous_lines[0])
    if previous_line.order >= verdict_line.order:
        raise ConvertError("无题号选择题判定块没有位于前锚之后")
    preceding = [
        line for line in layout_lines
        if (line.page_index == verdict_line.page_index
            and previous_line.order <= line.order < verdict_line.order)
    ]
    if not preceding or max(line.bbox[3] for line in preceding) >= verdict_line.bbox[1]:
        raise ConvertError("无题号选择题与前锚布局块重叠，拒绝切分")

    layout = imgorder.load_layout(local_dir)
    if layout is not None:
        boundary = verdict_line.bbox[1] * 1000
        for box in layout.images.values():
            if (int(box.page) == verdict_line.page_index
                    and float(box.bbox[1]) < boundary < float(box.bbox[3])):
                raise ConvertError("图片跨越无题号选择题边界，拒绝切分")

    anchor_key, anchor_positions = (
        collection_recovery._comparison_key_with_positions(anchor))
    verdict_key, _ = collection_recovery._comparison_key_with_positions(
        verdict_line.text)
    matcher = SequenceMatcher(None, anchor_key, verdict_key, autojunk=False)
    candidates = [
        block for block in matcher.get_matching_blocks()
        if block.size >= 24 and block.b <= 64
        and block.a >= max(32, len(anchor_key) // 5)
    ]
    if not candidates:
        raise ConvertError("四选项判定块不能唯一映射回局部 Markdown")
    match = min(candidates, key=lambda block: (block.b, block.a))
    seed = verdict_key[match.b:match.b + min(48, match.size)]
    if anchor_key.count(seed) != 1 or verdict_key.count(seed) != 1:
        raise ConvertError("四选项判定块在局部 Markdown 中不唯一")
    raw_cut = anchor_positions[match.a]
    suffix = anchor[raw_cut:].lstrip()
    verdicts = _choice_verdicts(suffix)
    if verdicts is None or verdicts != layout_verdicts:
        raise ConvertError("局部 Markdown 的四选项判定与布局块不一致")
    answer = "".join(label for label in "ABCD" if verdicts[label])
    if not answer or answer == "ABCD":
        raise ConvertError("四选项判定不能生成可信答案")
    recovered = f"{missing_number}. {answer}【详解】{suffix}"
    collection_recovery._validate_recovered_block(
        recovered, missing_number, 20, content_role="solution")
    cleaned = collection_recovery.trim_swallowed_solution_suffix(
        anchor, recovered, anchor_number=previous_number)
    collection_recovery.validate_recovered_anchor(
        anchor, cleaned, previous_number)
    return cleaned, recovered


_FORMULA_RE = re.compile(r"\$\$(?P<display>.*?)\$\$|\$(?P<inline>.*?)\$", re.S)


def _solution_head_answer(text: str, number: int) -> str | None:
    match = re.match(
        rf"^\s*{number}[ \t]*[.．、][ \t]*"
        r"(?P<answer>[A-DＡ-Ｄ](?:[ \t,，、/]*[A-DＡ-Ｄ]){0,3})"
        r"[ \t]*【(?:答案|详解|解析)】",
        text or "",
    )
    if match is None:
        return None
    return "".join(
        char.translate(str.maketrans("ＡＢＣＤ", "ABCD"))
        for char in match.group("answer") if char.upper() in "ABCDＡＢＣＤ")


def _formula_key(text: str) -> str:
    key, _ = collection_recovery._comparison_key_with_positions(text)
    # 同一速度符号，MinerU 的两轮 OCR 可能一轮输出 v_0，另一轮把印刷体 v
    # 误成希腊字母 \nu_0；prime 与撇号也常只保留一边。这里只用于三公式连续
    # 签名比对，不改写最终公式正文。
    key = key.replace("prime", "")
    key = re.sub(r"^nu(?=\d+$)", "v", key)
    return key


def _formula_spans(text: str):
    output = []
    for match in _FORMULA_RE.finditer(text or ""):
        raw = match.group("display") or match.group("inline") or ""
        key = _formula_key(raw)
        if len(key) >= 2:
            output.append((key, match.start(), match.end()))
    return output


def _is_proven_vertical_image_crop(source_path: Path,
                                   crop_path: Path) -> bool:
    """保守判断 ``crop_path`` 是否是 ``source_path`` 的竖向裁片。

    合集局部重识别会把跨越裁片高度的上一题配图再次导出，并把它排到下一题题尾。
    文件哈希因此不会相同，但两图在统一宽度后的墨迹应只存在竖向平移。这里只使用
    Pillow 自带运算：要求原始宽度近似、裁片有足够尺寸和墨迹、双向邻域覆盖均达到
    90%，且灰度差受限。任一证据不足就返回 False，不凭图片语义猜归属。
    """
    from PIL import Image, ImageChops, ImageFilter, ImageStat

    try:
        with Image.open(source_path) as source_image:
            source_image.load()
            source_size = source_image.size
            source = source_image.convert("L")
        with Image.open(crop_path) as crop_image:
            crop_image.load()
            crop_size = crop_image.size
            crop = crop_image.convert("L")
    except Exception:
        return False
    if (min(*source_size, *crop_size) < 64
            or not 0.85 <= crop_size[0] / source_size[0] <= 1.15):
        return False

    width = 96

    def _normalize(image):
        height = max(1, round(image.height * width / image.width))
        gray = image.resize((width, height), Image.Resampling.LANCZOS)
        mask = gray.point(lambda value: 255 if value < 220 else 0, "L")
        return gray, mask

    source_gray, source_mask = _normalize(source)
    crop_gray, crop_mask = _normalize(crop)
    if crop_mask.height < 32 or crop_mask.height > source_mask.height + 3:
        return False

    def _ink_count(mask) -> int:
        return mask.histogram()[255]

    def _intersection(left, right) -> int:
        return ImageChops.logical_and(
            left.convert("1"), right.convert("1")).convert("L").histogram()[255]

    source_dilated = source_mask.filter(ImageFilter.MaxFilter(5))
    best_score = 0.0
    best_difference = 255.0
    for height in range(max(32, crop_mask.height - 3),
                        crop_mask.height + 4):
        if height > source_mask.height:
            continue
        candidate_mask = crop_mask.resize(
            (width, height), Image.Resampling.NEAREST)
        candidate_gray = crop_gray.resize(
            (width, height), Image.Resampling.LANCZOS)
        candidate_ink = _ink_count(candidate_mask)
        if candidate_ink < 100:
            continue
        candidate_dilated = candidate_mask.filter(ImageFilter.MaxFilter(5))
        for top in range(source_mask.height - height + 1):
            source_part = source_mask.crop((0, top, width, top + height))
            source_ink = _ink_count(source_part)
            if source_ink < 100:
                continue
            source_near_candidate = (
                _intersection(source_part, candidate_dilated) / source_ink)
            candidate_near_source = _intersection(
                candidate_mask,
                source_dilated.crop((0, top, width, top + height)),
            ) / candidate_ink
            score = min(source_near_candidate, candidate_near_source)
            if score + 1e-9 < best_score:
                continue
            difference = ImageStat.Stat(ImageChops.difference(
                source_gray.crop((0, top, width, top + height)),
                candidate_gray,
            )).mean[0]
            if score > best_score or difference < best_difference:
                best_score = score
                best_difference = difference
    return best_score >= 0.90 and best_difference <= 24.0


def _recover_refined_solution_from_original_formula_suffix(
        original_anchor: str, local_missing: str, original_dir: Path,
        local_dir: Path, *, anchor_number: int,
        missing_number: int) -> tuple[str, str]:
    """用“同源裁图＋两公式连续签名”补齐缩窄裁片中的贴底解析。

    这种版面里，整本 OCR 把下一题正文吞进前锚点，缩窄裁片虽找回了强答案题头，
    却把上一题跨栏配图的下半截排在该题末尾。只有局部图被证明是前锚配图的竖向
    裁片，且局部全部公式在整本前锚中形成唯一连续序列时，才移除重复裁图并使用整
    本中的完整后半段；否则显式失败。
    """
    if missing_number != anchor_number + 1:
        raise ConvertError("缩窄裁片补齐只支持紧邻前锚点的下一题")
    local_answer = _solution_head_answer(local_missing, missing_number)
    anchor_answer = _solution_head_answer(original_anchor, anchor_number)
    if not local_answer or not anchor_answer:
        raise ConvertError("缩窄裁片补齐缺少强答案题头")
    anchor_blocks = collection_recovery._markdown_question_blocks(
        original_anchor)
    if (set(anchor_blocks) != {anchor_number}
            or len(anchor_blocks[anchor_number]) != 1):
        raise ConvertError("整本前锚不是唯一单题块，拒绝从中补齐缩窄裁片")

    model_paths = [
        path for path in Path(local_dir).glob("*_model.json")
        if path.is_file() and not path.is_symlink()
    ]
    if len(model_paths) != 1:
        raise ConvertError("缩窄裁片补齐缺少唯一 MinerU model.json")
    try:
        page_count, layout_lines = collection_recovery._read_layout_lines(
            model_paths[0])
    except collection_recovery.CollectionRecoveryError as exc:
        raise ConvertError(f"无法读取缩窄裁片布局：{exc}") from exc
    head_pattern = re.compile(
        rf"(?<![\d.．]){missing_number}[ \t]*[.．、][ \t]*"
        r"(?P<answer>[A-DＡ-Ｄ](?:[ \t,，、/]*[A-DＡ-Ｄ]){0,3})"
        r"[ \t]*【(?:答案|详解|解析)】")
    target_lines = []
    for line in layout_lines:
        match = head_pattern.search(line.text)
        if match is None:
            continue
        answer = "".join(
            char.translate(str.maketrans("ＡＢＣＤ", "ABCD"))
            for char in match.group("answer") if char.upper() in "ABCDＡＢＣＤ")
        if answer == local_answer:
            target_lines.append(line)
    if (len(target_lines) != 1
            or target_lines[0].page_index != page_count - 1
            or target_lines[0].bbox[3] < 0.975):
        raise ConvertError("缩窄裁片题头没有唯一贴近最后一页底边")

    local_formulas = _formula_spans(local_missing)
    source_formulas = _formula_spans(original_anchor)
    runs: list[tuple[int, int, int]] = []
    for left in range(len(local_formulas)):
        for right in range(len(source_formulas)):
            length = 0
            while (left + length < len(local_formulas)
                   and right + length < len(source_formulas)
                   and local_formulas[left + length][0]
                   == source_formulas[right + length][0]):
                length += 1
            if length >= 2:
                runs.append((left, right, length))
    best_length = max((item[2] for item in runs), default=0)
    best = [item for item in runs if item[2] == best_length]
    if len(best) != 1:
        raise ConvertError("缩窄裁片与整本解析没有唯一的两公式连续签名")
    left, right, length = best[0]
    if left != 0 or length != len(local_formulas):
        raise ConvertError("两公式签名没有覆盖缩窄裁片中的全部公式")
    formula_chars = sum(len(item[0]) for item in local_formulas)
    formula_prefix = local_missing[:local_formulas[0][1]]
    if (len(local_formulas) < 2 or formula_chars < 18
            or len(collection_recovery._comparison_key_with_positions(
                formula_prefix)[0]) > 24):
        raise ConvertError("缩窄裁片公式签名数量或信息量不足")

    source_boundary = source_formulas[right][1]
    source_tail_start = source_formulas[right + length - 1][2]
    source_tail = original_anchor[source_tail_start:]
    if source_boundary < max(64, len(original_anchor) // 4):
        raise ConvertError("公共公式序列没有位于整本前锚正文后段")
    if collection_recovery._markdown_question_blocks(source_tail):
        raise ConvertError("整本补全后缀中出现新的显式题号")
    local_after_formula = local_missing[local_formulas[-1][2]:]
    if (len(collection_recovery._comparison_key_with_positions(
            local_after_formula)[0]) < 12
            or len(collection_recovery._comparison_key_with_positions(
                source_tail)[0]) < 40):
        raise ConvertError("缩窄裁片或整本解析在公共公式后的正文不足")
    overlap = SequenceMatcher(
        None,
        collection_recovery._comparison_key_with_positions(
            local_after_formula)[0],
        collection_recovery._comparison_key_with_positions(source_tail)[0],
        autojunk=False,
    ).find_longest_match().size
    if overlap >= 32:
        raise ConvertError("缩窄裁片与整本补全文字存在长重叠，拒绝重复拼接")

    local_refs = _image_references(local_missing)
    source_refs = _image_references(original_anchor)
    if len(local_refs) != 1 or not source_refs:
        raise ConvertError("缩窄裁片补齐缺少唯一局部裁图或原始配图")
    if any(start < local_formulas[-1][2] for start, _, _, _ in local_refs):
        raise ConvertError("缩窄裁片图片位于公共公式之前，不能判为前题裁图")
    if any(end > source_boundary for _, end, _, _ in source_refs):
        raise ConvertError("整本前锚在公共公式之后仍有图片，不能证明图片归属")

    def _image_path(directory: Path, reference: str) -> Path:
        if (Path(reference).name != reference
                or any(mark in reference for mark in ("/", "\\", "%"))):
            raise ConvertError("合集恢复图片路径不安全")
        path = Path(directory) / "images" / reference
        if not path.is_file() or path.is_symlink():
            raise ConvertError(f"合集恢复图片不存在或不是普通文件：{reference}")
        return path

    local_path = _image_path(local_dir, local_refs[0][2])
    matched_sources = [
        reference for reference in source_refs
        if _is_proven_vertical_image_crop(
            _image_path(original_dir, reference[2]), local_path)
    ]
    if len(matched_sources) != 1:
        raise ConvertError("局部裁图不能唯一证明来自前锚点已有配图")

    local_text = local_missing
    for start, end, _, _ in reversed(local_refs):
        local_text = local_text[:start] + local_text[end:]
    local_text = local_text.rstrip()
    cleaned = original_anchor[:source_boundary].rstrip()
    recovered = local_text + source_tail.lstrip()
    collection_recovery._validate_recovered_block(
        cleaned, anchor_number, 20, content_role="solution")
    collection_recovery._validate_recovered_block(
        recovered, missing_number, 20, content_role="solution")
    collection_recovery.validate_recovered_anchor(
        original_anchor, cleaned, anchor_number)
    if (_solution_head_answer(recovered, missing_number) != local_answer
            or _solution_head_answer(cleaned, anchor_number) != anchor_answer):
        raise ConvertError("缩窄裁片补齐后答案题头发生变化")
    if len(_image_references(cleaned + recovered)) != len(source_refs):
        raise ConvertError("缩窄裁片补齐后原始图片引用数量不守恒")
    return cleaned, recovered


def _recover_clipped_solution_from_original(
        original_anchor: str, local_missing: str, local_dir: Path, *,
        anchor_number: int, missing_number: int) -> tuple[str, str]:
    """局部题头可信但正文贴底被截断时，用整本原文补齐其后半段。"""
    local_answer = _solution_head_answer(local_missing, missing_number)
    anchor_answer = _solution_head_answer(original_anchor, anchor_number)
    if not local_answer or not anchor_answer:
        raise ConvertError("贴底解析缺少强答案题头")

    model_paths = [
        path for path in Path(local_dir).glob("*_model.json")
        if path.is_file() and not path.is_symlink()
    ]
    if len(model_paths) != 1:
        raise ConvertError("贴底解析缺少唯一 MinerU model.json")
    try:
        page_count, layout_lines = collection_recovery._read_layout_lines(
            model_paths[0])
    except collection_recovery.CollectionRecoveryError as exc:
        raise ConvertError(f"无法读取贴底解析布局：{exc}") from exc
    target_lines = [
        line for line in layout_lines
        if collection_structure._question_number(line.text) == missing_number
    ]
    if (len(target_lines) != 1
            or target_lines[0].page_index != page_count - 1
            or target_lines[0].bbox[3] < 0.995):
        raise ConvertError("局部解析没有贴到最后一页底边，不能走截断补齐")

    verdict_re = re.compile(
        r"(?<![A-Za-z])([A-DＡ-Ｄ])[ \t]*"
        r"(正确|错误|不正确|错)(?![误])")
    hits = list(verdict_re.finditer(original_anchor))
    sequences = []
    for index in range(max(0, len(hits) - 3)):
        group = hits[index:index + 4]
        labels = [item.group(1).translate(
            str.maketrans("ＡＢＣＤ", "ABCD")) for item in group]
        if labels == list("ABCD"):
            sequences.append(group)
    if len(sequences) != 1:
        raise ConvertError("原整本前锚中没有唯一完整的 A—D 结束边界")
    sequence = sequences[0]
    correct = "".join(
        item.group(1).translate(str.maketrans("ＡＢＣＤ", "ABCD"))
        for item in sequence if item.group(2) == "正确")
    if correct != anchor_answer:
        raise ConvertError("原整本前锚的 A—D 判定与答案题头不一致")
    boundary = sequence[-1].end()
    while (boundary < len(original_anchor)
           and original_anchor[boundary] in "。；;，, \t\r\n"):
        boundary += 1
    source_suffix = original_anchor[boundary:]
    if len(collection_recovery._comparison_key_with_positions(source_suffix)[0]) < 40:
        raise ConvertError("原整本中被吞入的后续解析正文不足")
    if (_image_references(local_missing) or _image_references(source_suffix)):
        raise ConvertError("贴底解析含图片，当前不能证明跨来源图片顺序")

    local_formulas = _formula_spans(local_missing)
    source_formulas = _formula_spans(source_suffix)
    runs: list[tuple[int, int, int]] = []
    for left in range(len(local_formulas)):
        for right in range(len(source_formulas)):
            length = 0
            while (left + length < len(local_formulas)
                   and right + length < len(source_formulas)
                   and local_formulas[left + length][0]
                   == source_formulas[right + length][0]):
                length += 1
            if length >= 3:
                runs.append((left, right, length))
    best_length = max((item[2] for item in runs), default=0)
    best = [item for item in runs if item[2] == best_length]
    if len(best) != 1:
        raise ConvertError("局部与整本解析没有唯一的三公式连续签名")
    left, right, length = best[0]
    combined_formula_chars = sum(
        len(local_formulas[left + offset][0]) for offset in range(length))
    if right > 1 or combined_formula_chars < 12:
        raise ConvertError("三公式签名不在整本被吞正文开头或信息量不足")
    source_tail_start = source_formulas[right + length - 1][2]
    source_tail = source_suffix[source_tail_start:]
    if len(collection_recovery._comparison_key_with_positions(source_tail)[0]) < 30:
        raise ConvertError("整本解析在公共公式后的补全文字不足")
    local_after_formula = local_missing[
        local_formulas[left + length - 1][2]:]
    overlap = SequenceMatcher(
        None,
        collection_recovery._comparison_key_with_positions(local_after_formula)[0],
        collection_recovery._comparison_key_with_positions(source_tail)[0],
        autojunk=False,
    ).find_longest_match().size
    if overlap >= 32:
        raise ConvertError("局部与整本补全文字存在长重叠，拒绝盲目拼接")

    cleaned = original_anchor[:boundary].rstrip()
    recovered = local_missing.rstrip() + source_tail.lstrip()
    collection_recovery._validate_recovered_block(
        cleaned, anchor_number, 20, content_role="solution")
    collection_recovery._validate_recovered_block(
        recovered, missing_number, 20, content_role="solution")
    collection_recovery.validate_recovered_anchor(
        original_anchor, cleaned, anchor_number)
    if _solution_head_answer(recovered, missing_number) != local_answer:
        raise ConvertError("补齐后解析题头发生变化")
    return cleaned, recovered


def _recognize_collection_recovery_crop(crop_path: Path, recovery_dir: Path,
                                        cfg, *, note_sink=None,
                                        label: str = ""):
    """强制 OCR 一个稳定裁片；原文、图片与 batch 续传状态全部可复用。"""
    recovery_dir = Path(recovery_dir)
    recovery_dir.mkdir(parents=True, exist_ok=True)
    if recovery_dir.is_symlink():
        raise ConvertError("合集局部 OCR 缓存目录不能是符号链接")
    raw_path = recovery_dir / "recovered_raw.md"
    meta_path = recovery_dir / "recovered_meta.json"
    extract_dir = recovery_dir / "extract"
    crop_sha = _file_sha256(crop_path)
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        cached = raw_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError, json.JSONDecodeError):
        meta, cached = None, ""
    if (isinstance(meta, dict)
            and meta.get("version") == _COLLECTION_RECOVERY_VERSION
            and meta.get("crop_sha256") == crop_sha
            and meta.get("mineru_model_version") == cfg.mineru_model_version
            and cached.strip() and extract_dir.is_dir()
            and not extract_dir.is_symlink()):
        if note_sink is not None:
            note_sink(f"{label}已复用局部 MinerU 恢复缓存")
        return cached, extract_dir

    _ensure_src_on_path()
    from src.mineru_client import MineruClient

    staging = recovery_dir / f".extract-{uuid.uuid4().hex}"
    staging.mkdir()
    try:
        raw, _ = ocr_pool.run(
            OCR_MINERU,
            lambda token: MineruClient(
                token, cfg.mineru_model_version).parse_pdf(
                    crop_path, extract_dir=staging, force_ocr=True,
                    resume_dir=recovery_dir, resume_key="forced-ocr"),
            fallback=cfg.mineru_token,
        )
        raw = _clean_mineru_text(
            raw, crop_path, label=label, note_sink=note_sink)
        raw = _repair_choice_images(raw, staging, note_sink, label=label)
        _ensure_raw_text(raw, crop_path, label=label, ocr_backend=OCR_MINERU)
        if extract_dir.exists():
            if extract_dir.is_symlink() or not extract_dir.is_dir():
                raise ConvertError("合集局部 OCR 解压目标不是普通目录")
            shutil.rmtree(extract_dir)
        shutil.move(str(staging), str(extract_dir))
        marker = uuid.uuid4().hex
        raw_tmp = recovery_dir / f".recovered_raw.{marker}.tmp"
        meta_tmp = recovery_dir / f".recovered_meta.{marker}.tmp"
        try:
            raw_tmp.write_text(raw, encoding="utf-8")
            meta_tmp.write_text(json.dumps({
                "version": _COLLECTION_RECOVERY_VERSION,
                "crop_sha256": crop_sha,
                "mineru_model_version": cfg.mineru_model_version,
            }, ensure_ascii=False), encoding="utf-8")
            os.replace(raw_tmp, raw_path)
            os.replace(meta_tmp, meta_path)
        finally:
            raw_tmp.unlink(missing_ok=True)
            meta_tmp.unlink(missing_ok=True)
        return raw, extract_dir
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _recover_collection_unit_markdown(
        source_pdf: Path, workspace: Path, model_document,
        unit, target_numbers, *, previous_unit=None, next_unit=None,
        replace_existing: bool, cfg, source_sha256: str,
        content_role: str, note_sink=None, label: str = "",
        crop_recognizer=None,
        ) -> tuple[str, int]:
    """裁片恢复一个单元，并同时替换前锚点以清掉被它吞入的缺题。"""
    targets = sorted(set(int(number) for number in target_numbers))
    if not targets:
        return unit.markdown, 0
    plans = collection_recovery.plan_gap_crops(
        model_document, unit, targets,
        previous_unit=previous_unit, next_unit=next_unit,
        max_missing_numbers=collection_recovery.MAX_MISSING_NUMBERS,
        allow_cross_page=True,
        max_pages_per_gap=collection_recovery.MAX_PAGES_PER_GAP,
        replace_existing=replace_existing,
    )
    source_image_layout = imgorder.load_layout(workspace) if replace_existing else None
    layout_unit = (collection_recovery._match_title_unit(model_document, unit)
                   if replace_existing else None)
    next_layout_unit = (
        collection_recovery._match_title_unit(model_document, next_unit)
        if replace_existing and next_unit is not None else None)
    rebuilt = unit.markdown
    recovered_count = 0
    for plan in plans:
        key = _collection_recovery_key(source_sha256, plan)
        recovery_dir = Path(workspace) / _COLLECTION_RECOVERY_ROOT / key
        crops = collection_recovery.export_gap_crops(
            source_pdf, [plan], recovery_dir)
        crop = crops[0]
        if crop_recognizer is None:
            local_raw, local_dir = _recognize_collection_recovery_crop(
                crop.path, recovery_dir, cfg, note_sink=note_sink,
                label=f"{label}“{unit.title}”第 "
                      f"{plan.missing_numbers[0]}—{plan.missing_numbers[-1]} 题")
        else:
            local_raw, local_dir = crop_recognizer(crop, recovery_dir)

        local_raw = collection_recovery.trim_trailing_next_unit_title(
            local_raw, next_unit.title if next_unit is not None else None)
        # 必须在分别抽取前锚点和缺题之前统一补题首边界。若先取锚点、后在另一
        # 次选择中才把 ``16. (1)`` 拆出来，先前取得的锚点仍会吞着第 16 题正文。
        local_raw = collection_recovery.normalize_recovered_question_heads(
            local_raw, [plan.previous_number, *plan.missing_numbers],
            content_role=content_role)

        current_unit = dataclasses.replace(unit, markdown=rebuilt)
        current_blocks = _collection_unit_blocks(
            current_unit, allow_out_of_order=True)
        current_by_number = {block.number: block for block in current_blocks}
        spans = _exact_block_spans(rebuilt, current_blocks)
        if spans is None:
            raise ConvertError(
                "合集局部恢复时无法按唯一、连续的原始题号边界定位题块")
        previous = current_by_number.get(plan.previous_number)
        if previous is None:
            # 首题缺失需跨单元同时替换前锚点，当前宁可显式停止，
            # 不把上一单元末题错插进本单元。
            raise ConvertError(
                f"合集单元“{unit.title}”的恢复区间缺少本单元前锚点")
        # 前锚点必须来自完整裁片并单独验真。若完整裁片仍漏掉中部题号，
        # 可再送几个只缩顶部、始终保留后一题边界的单页变体给 MinerU；
        # 变体只能提供缺题，绝不能取代前锚点证据。
        selected = collection_recovery.select_recovered_questions(
            local_raw, [plan.previous_number], content_role=content_role)
        selected_dirs = {plan.previous_number: local_dir}
        used_refinement = False
        try:
            missing_selected = collection_recovery.select_recovered_questions(
                local_raw, plan.missing_numbers, content_role=content_role)
            missing_dir = local_dir
        except collection_recovery.CollectionRecoveryError as primary_error:
            # 缩窄裁片只能补“解析题号行被漏掉”这一种版面错误。重复题号、正文
            # 不足、题干缺失、多缺题或多页区间都不能靠换裁片掩盖。
            primary_blocks = collection_recovery._markdown_question_blocks(
                local_raw)
            missing_selected = None
            missing_dir = None
            verdict_error = None
            can_try_verdict = (
                content_role == "solution"
                and not replace_existing
                and len(plan.missing_numbers) == 1
                and not primary_blocks.get(plan.missing_numbers[0])
            )
            if can_try_verdict:
                try:
                    cleaned_anchor, recovered_choice = (
                        _recover_unheaded_choice_solution(
                            local_raw, local_dir,
                            previous_number=plan.previous_number,
                            missing_number=plan.missing_numbers[0]))
                except ConvertError as exc:
                    verdict_error = exc
                else:
                    selected[plan.previous_number] = cleaned_anchor
                    missing_selected = {
                        plan.missing_numbers[0]: recovered_choice}
                    missing_dir = local_dir
                    if note_sink is not None:
                        note_sink(
                            f"{label}“{unit.title}”第 "
                            f"{plan.missing_numbers[0]} 题题号被漏掉，已按唯一"
                            " A—D 判定块和 MinerU 坐标机械恢复")
            can_refine = (
                content_role == "solution"
                and not replace_existing
                and len(plan.missing_numbers) == 1
                and len(plan.slices) == 1
                and not primary_blocks.get(plan.missing_numbers[0])
            )
            if missing_selected is not None:
                can_refine = False
            elif not can_refine:
                if verdict_error is not None:
                    raise collection_recovery.CollectionRecoveryError(
                        f"{primary_error}；无题号选择题恢复也失败："
                        f"{verdict_error}")
                raise primary_error
            if not can_refine:
                pass
            else:
                try:
                    refined_paths = (
                        collection_recovery.export_vertical_suffix_crops(
                            crop.path, recovery_dir / "refined-crops"))
                except collection_recovery.CollectionRecoveryError:
                    # 多页裁片等无法安全缩窄的情况保留原始、最具体的失败原因。
                    raise primary_error
                refinement_errors: list[str] = []
                for refined_index, refined_path in enumerate(refined_paths, 1):
                    refined_dir = recovery_dir / f"refined-{refined_index:02d}"
                    refined_crop = collection_recovery.RecoveryCrop(
                        plan, refined_path)
                    if crop_recognizer is None:
                        refined_raw, refined_extract = (
                            _recognize_collection_recovery_crop(
                                refined_path, refined_dir, cfg,
                                note_sink=note_sink,
                                label=f"{label}“{unit.title}”第 "
                                      f"{plan.missing_numbers[0]}—"
                                      f"{plan.missing_numbers[-1]} 题缩窄裁片"
                            ))
                    else:
                        refined_raw, refined_extract = crop_recognizer(
                            refined_crop, refined_dir)
                    refined_raw = (
                        collection_recovery.normalize_recovered_question_heads(
                            refined_raw, plan.missing_numbers,
                            content_role=content_role))
                    try:
                        missing_selected = (
                            collection_recovery.select_recovered_questions(
                                refined_raw, plan.missing_numbers,
                                content_role=content_role))
                    except collection_recovery.CollectionRecoveryError as exc:
                        refinement_errors.append(str(exc))
                        continue
                    missing_dir = refined_extract
                    used_refinement = True
                    if note_sink is not None:
                        note_sink(
                            f"{label}“{unit.title}”完整裁片仍漏题号，已用保持"
                            f"后一题边界的第 {refined_index} 个缩窄裁片恢复")
                    break
                if missing_selected is None or missing_dir is None:
                    detail = refinement_errors[-1] if refinement_errors else str(
                        primary_error)
                    raise collection_recovery.CollectionRecoveryError(
                        f"{primary_error}；缩窄裁片仍未恢复：{detail}")
        selected.update(missing_selected)
        selected_dirs.update({number: missing_dir
                              for number in missing_selected})
        used_clipped_merge = False
        used_refined_formula_merge = False
        if (content_role == "solution" and not used_refinement
                and len(plan.missing_numbers) == 1):
            missing_number = plan.missing_numbers[0]
            try:
                clean_original, complete_missing = (
                    _recover_clipped_solution_from_original(
                        previous.text, selected[missing_number], local_dir,
                        anchor_number=plan.previous_number,
                        missing_number=missing_number))
            except ConvertError:
                pass
            else:
                selected[plan.previous_number] = clean_original
                selected[missing_number] = complete_missing
                selected_dirs[plan.previous_number] = workspace
                selected_dirs[missing_number] = workspace
                used_clipped_merge = True
                if note_sink is not None:
                    note_sink(
                        f"{label}“{unit.title}”第 {missing_number} 题局部题头"
                        "可信但正文贴底，已用三公式连续签名拼回整本中的完整后半段")
        if (content_role == "solution" and used_refinement
                and len(plan.missing_numbers) == 1):
            missing_number = plan.missing_numbers[0]
            try:
                clean_original, complete_missing = (
                    _recover_refined_solution_from_original_formula_suffix(
                        previous.text, selected[missing_number], workspace,
                        missing_dir, anchor_number=plan.previous_number,
                        missing_number=missing_number))
            except ConvertError:
                pass
            else:
                selected[plan.previous_number] = clean_original
                selected[missing_number] = complete_missing
                selected_dirs[plan.previous_number] = workspace
                selected_dirs[missing_number] = workspace
                used_refined_formula_merge = True
                if note_sink is not None:
                    note_sink(
                        f"{label}“{unit.title}”第 {missing_number} 题缩窄裁片"
                        "贴底，已按同源裁图和两公式连续签名拼回完整解析")
        if not (used_clipped_merge or used_refined_formula_merge):
            selected = _attach_proven_leading_solution_images(
                local_raw, selected, local_dir,
                missing_numbers=plan.missing_numbers,
                content_role=content_role)
        collection_recovery.validate_recovered_anchor(
            previous.text, selected[plan.previous_number],
            plan.previous_number)
        if used_refinement and not used_refined_formula_merge:
            missing_number = plan.missing_numbers[0]
            selected[plan.previous_number] = (
                collection_recovery.trim_swallowed_solution_suffix(
                    selected[plan.previous_number],
                    selected[missing_number],
                    anchor_number=plan.previous_number))
            collection_recovery.validate_recovered_anchor(
                previous.text, selected[plan.previous_number],
                plan.previous_number)
        rewritten = {
            number: collection_recovery.copy_recovery_images(
                text, selected_dirs[number], workspace)
            for number, text in selected.items()
        }
        relocation_messages: list[str] = []
        if replace_existing:
            relocated_by_target: dict[int, list[str]] = {}
            for number in plan.missing_numbers:
                old = current_by_number.get(number)
                raw_source_text = (rebuilt[slice(*spans[number])]
                                   if old is not None else "")
                comparison_text, relocations = _relocate_out_of_crop_images(
                    old.text if old is not None else "",
                    question_number=number, plan=plan,
                    source_layout=source_image_layout, layout_unit=layout_unit,
                    next_layout_unit=next_layout_unit,
                    existing_numbers=current_by_number,
                    raw_source_text=raw_source_text,
                )
                if old is None or not _alternate_block_is_better(
                        dataclasses.replace(old, text=comparison_text),
                        dataclasses.replace(old, text=rewritten[number]),
                        require_matching_stem=bool(relocations)):
                    raise ConvertError(
                        f"局部 OCR 没有为“{unit.title}”第 {number} 题"
                        "提供可证明的完整性增益，已拒绝覆盖")
                for owner, refs in relocations.items():
                    relocated_by_target.setdefault(owner, []).extend(refs)
                if relocations:
                    details = "、".join(
                        f"第 {owner} 题 {len(refs)} 张"
                        for owner, refs in sorted(relocations.items()))
                    message = (
                        f"{label}“{unit.title}”按原版面坐标将第 {number} 题"
                        f"裁片外图片归还至{details}")
                    relocation_messages.append(message)
            for owner, refs in relocated_by_target.items():
                base = rewritten.get(owner, rebuilt[slice(*spans[owner])])
                rewritten[owner] = base.rstrip() + "\n\n" + "\n\n".join(refs)
        rebuilt_next = _rebuild_collection_unit(
            rebuilt, current_blocks, spans, rewritten)
        if (replace_existing
                and _collection_block_image_count(rebuilt_next)
                < _collection_block_image_count(rebuilt)):
            raise ConvertError(
                f"合集单元“{unit.title}”局部恢复后的图片引用总数减少，"
                "已拒绝覆盖")
        rebuilt = rebuilt_next
        check_unit = dataclasses.replace(unit, markdown=rebuilt)
        check_numbers = [
            block.number for block in _collection_unit_blocks(
                check_unit, allow_out_of_order=True)]
        expected_numbers = sorted(set(current_by_number) | set(rewritten))
        if sorted(check_numbers) != expected_numbers:
            raise ConvertError(
                f"局部 OCR 替换后题号序列不守恒：预期 "
                f"{expected_numbers}，实际 {check_numbers}")
        for message in relocation_messages:
            logger.info("[OK] %s", message)
            if note_sink is not None:
                note_sink(message)
        recovered_count += 1
    return rebuilt, recovered_count


def _recover_mineru_collection(exam_raw: str, solution_raw: str | None, *,
                               exam_path: Path, solution_path: Path | None,
                               exam_dir: Path, solution_dir: Path | None,
                               cfg_getter, note_sink=None,
                               crop_recognizer=None):
    """以题干断号、机械配对缺解析和强弱题为证据，对 MinerU 合集做有界局部恢复。"""
    if exam_path.suffix.lower() != ".pdf" or (
            solution_path is not None and solution_path.suffix.lower() != ".pdf"):
        # 无缺口的 Word/图片合集不会进入下面的裁片分支。
        source_pdfs = False
    else:
        source_pdfs = True

    crop_count = 0
    exam_units = collection_structure.split_markdown_units_for_recovery(
        exam_raw, label="题干合集恢复前")
    exam_gaps = [
        _collection_number_gaps(unit, allow_out_of_order=True)
        for unit in exam_units]
    exam_weak = [
        _weak_collection_question_numbers(unit, allow_out_of_order=True)
        for unit in exam_units]
    if any(exam_gaps) or any(exam_weak):
        if not source_pdfs:
            raise ConvertError("合集存在结构缺口，但局部恢复目前只支持 PDF 源文件")
        exam_model = collection_recovery.load_layout_document(
            _collection_model_json(
                exam_dir, exam_path, reference_markdown=exam_raw))
        exam_sha = _file_sha256(exam_path)
        cfg = cfg_getter()
        for index in range(len(exam_units)):
            units_now = collection_structure.split_markdown_units_for_recovery(
                exam_raw, label="题干合集局部恢复")
            unit = units_now[index]
            gaps = _collection_number_gaps(unit, allow_out_of_order=True)
            if gaps:
                rebuilt, used = _recover_collection_unit_markdown(
                    exam_path, exam_dir, exam_model, unit, gaps,
                    previous_unit=units_now[index - 1] if index else None,
                    next_unit=units_now[index + 1]
                    if index + 1 < len(units_now) else None,
                    replace_existing=False, cfg=cfg,
                    source_sha256=exam_sha, content_role="stem",
                    note_sink=note_sink,
                    label="题干合集", crop_recognizer=crop_recognizer)
                exam_raw = _replace_collection_unit(
                    exam_raw, unit.markdown, rebuilt)
                crop_count += used
            units_now = collection_structure.split_markdown_units_for_recovery(
                exam_raw, label="题干合集弱题恢复")
            unit = units_now[index]
            weak = _weak_collection_question_numbers(
                unit, allow_out_of_order=True)
            if weak:
                rebuilt, used = _recover_collection_unit_markdown(
                    exam_path, exam_dir, exam_model, unit, weak,
                    previous_unit=units_now[index - 1] if index else None,
                    next_unit=units_now[index + 1]
                    if index + 1 < len(units_now) else None,
                    replace_existing=True, cfg=cfg,
                    source_sha256=exam_sha, content_role="stem",
                    note_sink=note_sink,
                    label="题干合集", crop_recognizer=crop_recognizer)
                exam_raw = _replace_collection_unit(
                    exam_raw, unit.markdown, rebuilt)
                crop_count += used
            if crop_count > _COLLECTION_RECOVERY_MAX_CROPS:
                raise ConvertError(
                    f"合集局部 OCR 裁片超过 {_COLLECTION_RECOVERY_MAX_CROPS} 个，"
                    "已停止自动提交")

    if solution_raw is not None:
        # 整本解析中常见 ``...上一题末尾。19. AC【详解】...`` 或
        # ``...上一题末尾。12. (1)...``。先按题干侧已确认的完整题号集合统一补
        # 换行，可直接消除这类结构缺口，也避免为本来完整的正文重复提交局部 OCR。
        initial_pairs = collection_structure.pair_markdown_collections(
            exam_raw, solution_raw)
        normalized_solution = solution_raw
        normalized_count = 0
        for pair in initial_pairs:
            expected_numbers = [
                block.number for block in _collection_unit_blocks(
                    pair.exam, allow_out_of_order=True)
                if isinstance(block.number, int)
            ]
            repaired = collection_recovery.normalize_recovered_question_heads(
                pair.solution.markdown, expected_numbers,
                content_role="solution")
            if repaired != pair.solution.markdown:
                normalized_solution = _replace_collection_unit(
                    normalized_solution, pair.solution.markdown, repaired)
                normalized_count += 1
        solution_raw = normalized_solution
        if normalized_count and note_sink is not None:
            note_sink(
                f"解析合集已按题干题号机械补回 {normalized_count} 个专题中的"
                "行内题首边界")
        pairs = collection_structure.pair_markdown_collections(
            exam_raw, solution_raw)
        missing_by_unit: list[list[int]] = []
        for pair in pairs:
            missing, _ = _mechanical_missing_solution_numbers(
                pair.exam, pair.solution)
            missing_by_unit.append(missing)
        if any(missing_by_unit):
            if not source_pdfs:
                raise ConvertError("解析合集存在配对缺口，但局部恢复目前只支持 PDF")
            solution_model = collection_recovery.load_layout_document(
                _collection_model_json(
                    solution_dir, solution_path,
                    reference_markdown=solution_raw))
            solution_sha = _file_sha256(solution_path)
            cfg = cfg_getter()
            for index in range(len(pairs)):
                pairs_now = collection_structure.pair_markdown_collections(
                    exam_raw, solution_raw)
                pair = pairs_now[index]
                missing, _ = _mechanical_missing_solution_numbers(
                    pair.exam, pair.solution)
                if not missing:
                    continue
                solutions_now = [item.solution for item in pairs_now]
                rebuilt, used = _recover_collection_unit_markdown(
                    solution_path, solution_dir, solution_model,
                    pair.solution, missing,
                    previous_unit=solutions_now[index - 1] if index else None,
                    next_unit=solutions_now[index + 1]
                    if index + 1 < len(solutions_now) else None,
                    replace_existing=False, cfg=cfg,
                    source_sha256=solution_sha, content_role="solution",
                    note_sink=note_sink,
                    label="解析合集", crop_recognizer=crop_recognizer)
                solution_raw = _replace_collection_unit(
                    solution_raw, pair.solution.markdown, rebuilt)
                crop_count += used
                if crop_count > _COLLECTION_RECOVERY_MAX_CROPS:
                    raise ConvertError(
                        f"合集局部 OCR 裁片超过 "
                        f"{_COLLECTION_RECOVERY_MAX_CROPS} 个，已停止自动提交")

    # 最终门：题干必须连续，解析必须与题干题号逐组完全一致，
    # 弱题也不能在一次局部 OCR 后仍带病自动入库。
    final_pairs = collection_structure.pair_markdown_collections(
        exam_raw, solution_raw)
    problems: list[str] = []
    for index, pair in enumerate(final_pairs, 1):
        gaps = _collection_number_gaps(
            pair.exam, allow_out_of_order=True)
        weak = _weak_collection_question_numbers(
            pair.exam, allow_out_of_order=True)
        if gaps:
            problems.append(f"第 {index} 组题干缺 {','.join(map(str, gaps))}")
        if weak:
            problems.append(f"第 {index} 组弱题 {','.join(map(str, weak))}")
        if pair.solution is not None:
            missing, pairing = _mechanical_missing_solution_numbers(
                pair.exam, pair.solution)
            if missing:
                problems.append(
                    f"第 {index} 组解析缺 " + ",".join(map(str, missing)))
            if pairing.orphan_solutions:
                problems.append(
                    f"第 {index} 组有 {len(pairing.orphan_solutions)} 个孤立解析块")
            if pairing.missing_numbers or pairing.number_gaps:
                problems.append(f"第 {index} 组题号坐标不完整")
    if problems:
        raise ConvertError("合集局部 MinerU 恢复后仍有结构缺口："
                           + "；".join(problems[:12]))
    if crop_count and note_sink is not None:
        note_sink(f"合集已用 {crop_count} 个可续传的局部 MinerU 裁片"
                  "恢复缺题/弱题，并通过逐组题号配对门")
    return exam_raw, solution_raw


def recognize_collection_units(exam_path, solution_path=None,
                               mineru_token: str = "", *, note_sink=None,
                               ocr_backend: str = OCR_MINERU,
                               doc2x_api_key: str = "",
                               cache_dirs=None,
                               max_units: int | None = None) -> list[dict]:
    """题干/解析合集各整本 OCR 一次，再在 Markdown 中分组。

    返回的每个元素是独立单元工作区：``raw_path`` 保留该组原文，
    ``workspace_dir/images`` 只含该组引用的图片。下游因此能并发切题、
    独立重转，也不会在第一组收尾时删掉其他组的图。
    """
    exam_path = Path(exam_path).resolve()
    solution_path = Path(solution_path).resolve() if solution_path else None
    if not exam_path.is_file():
        raise ConvertError(f"题干合集不存在: {exam_path}")
    if solution_path is not None and not solution_path.is_file():
        raise ConvertError(f"解析合集不存在: {solution_path}")
    backend = normalize_ocr_backend(ocr_backend)

    workspaces = _collection_cache_workspaces(
        solution_path is not None, cache_dirs)
    exam_dir = workspaces[0]
    solution_dir = workspaces[1] if solution_path is not None else None
    exam_cached = _read_collection_cache(exam_dir)
    solution_cached = (_read_collection_cache(solution_dir)
                       if solution_dir is not None else None)

    # 两侧都已有完整缓存时不再要求凭证，更不能再触发付费 OCR。只要有
    # 一侧缺缓存，配置才是实际需要的。
    cfg = None
    if exam_cached is None or (solution_path is not None
                               and solution_cached is None):
        _ensure_src_on_path()
        with _alpha_cwd():
            cfg = _load_config_for_user(
                mineru_token, require_mineru=(backend == OCR_MINERU))

    def _get_cfg():
        nonlocal cfg
        if cfg is None:
            _ensure_src_on_path()
            with _alpha_cwd():
                cfg = _load_config_for_user(
                    mineru_token, require_mineru=(backend == OCR_MINERU))
        return cfg

    created_dirs: list[Path] = []
    succeeded = False
    try:
        def _recognize_side(source: Path, extract_dir: Path, label: str,
                            cached):
            if cached is not None:
                raw, meta = cached
                if backend == OCR_MINERU:
                    side = "exam" if label == "题干合集" else "solution"
                    restored = _restore_collection_image_namespace(
                        raw, extract_dir, side)
                    repaired = _repair_choice_images(
                        restored, extract_dir, note_sink, label=label)
                    if repaired != raw:
                        _write_collection_cache(extract_dir, repaired, meta)
                    raw = repaired
                else:
                    repaired, moved, choices = doc2x_client.repair_cached_markdown(
                        raw, extract_dir)
                    if repaired != raw:
                        _write_collection_cache(extract_dir, repaired, meta)
                        raw = repaired
                    if note_sink is not None and (moved or choices):
                        note_sink(
                            f"（{label}）已按 Doc2X 页面坐标移回 {moved} 张跨题图片、"
                            f"恢复 {choices} 组四图选项")
                if note_sink is not None:
                    note_sink(f"（{label}）已复用整本识别缓存，未再次调用 OCR")
                return raw, meta
            prepared = _prep_for_ocr(source, backend, work_dir=extract_dir)
            raw, _, meta = _parse_with_ocr_backend(
                prepared, extract_dir, cfg, ocr_backend=backend,
                doc2x_api_key=doc2x_api_key, note_sink=note_sink,
                label=label, collection=True)
            raw = _clean_mineru_text(
                raw, source, label=label, note_sink=note_sink)
            _ensure_raw_text(
                raw, source, label=label, ocr_backend=backend)
            _write_collection_cache(extract_dir, raw, meta)
            return raw, meta

        if solution_path is not None:
            with ThreadPoolExecutor(max_workers=2) as pool:
                exam_future = pool.submit(
                    _recognize_side, exam_path, exam_dir, "题干合集",
                    exam_cached)
                solution_future = pool.submit(
                    _recognize_side, solution_path, solution_dir, "解析合集",
                    solution_cached)
                exam_raw, exam_meta = exam_future.result()
                solution_raw, solution_meta = solution_future.result()
        else:
            exam_raw, exam_meta = _recognize_side(
                exam_path, exam_dir, "题干合集", exam_cached)
            solution_raw = None
            solution_meta = None

        if backend == OCR_MINERU:
            exam_raw, solution_raw = _recover_mineru_collection(
                exam_raw, solution_raw,
                exam_path=exam_path, solution_path=solution_path,
                exam_dir=exam_dir, solution_dir=solution_dir,
                cfg_getter=_get_cfg, note_sink=note_sink)
            # 局部恢复通过完整性门后先持久化未命名空间化的单侧原文。
            # 若进程在下面的双侧图片归并前退出，下次仍能继续修复，
            # 不会重提整本或已完成的裁片。
            _write_collection_cache(exam_dir, exam_raw, exam_meta)
            if solution_raw is not None:
                _write_collection_cache(solution_dir, solution_raw, solution_meta)

        if solution_raw is not None:
            exam_raw, solution_raw = _merge_dual_image_trees(
                exam_raw, solution_raw, exam_dir, solution_dir)
            # 合并后的引用已带 exam_/solution_ 前缀，覆盖缓存使重试幂等。
            _write_collection_cache(exam_dir, exam_raw, exam_meta)
            _write_collection_cache(solution_dir, solution_raw, solution_meta)

        try:
            pairs = collection_structure.pair_markdown_collections(
                exam_raw, solution_raw)
        except collection_structure.CollectionStructureError as exc:
            raise ConvertError(str(exc)) from exc
        if max_units is not None and len(pairs) > max(0, int(max_units)):
            raise ConvertError(
                f"合集识别出 {len(pairs)} 组，超过本批剩余上限 "
                f"{max(0, int(max_units))} 组")

        units: list[dict] = []
        for index, pair in enumerate(pairs, 1):
            combined = pair.exam.markdown.rstrip()
            if pair.solution is not None:
                combined += ("\n\n# 参考答案与解析\n\n"
                             + pair.solution.markdown.lstrip())

            scope = f"collection_unit_{uuid.uuid4().hex}"
            unit_dir = _raw_md_dir(scope)
            unit_dir.mkdir(parents=True, exist_ok=False)
            created_dirs.append(unit_dir)
            _copy_collection_images(combined, exam_dir, unit_dir)
            raw_path = unit_dir / f"{scope}_raw.md"
            raw_path.write_text(combined, encoding="utf-8")
            units.append({
                "title": pair.title,
                "raw_path": str(raw_path),
                "workspace_dir": str(unit_dir),
                "scope": scope,
                "include_solution": pair.solution is not None,
                "ocr_backend": backend,
                "ocr_meta": {"exam": exam_meta,
                             "solution": solution_meta},
                # 成功返回到上层前也不能删整本缓存：若进程在“创建单元”与
                # “持久化子组”之间退出，父任务仍要靠它免付费恢复。
                "collection_cache_dirs": (
                    [str(path) for path in workspaces] if index == 1 else []),
            })
        succeeded = True
        return units
    except CollectionRecognitionError:
        raise
    except ConvertError as exc:
        raise CollectionRecognitionError(str(exc), workspaces) from exc
    except Exception as exc:
        raise CollectionRecognitionError(
            f"合集整本识别或自动分组失败: "
            f"{type(exc).__name__}: {exc}", workspaces) from exc
    finally:
        # 无论成功失败，整本缓存都交还上层：上层必须先持久化子组，再回收
        # 缓存，才能覆盖进程在两步之间退出的恢复窗口。
        if not succeeded:
            for directory in created_dirs:
                _remove_ocr_workspace(directory, unit_only=True)


def _export_choice_refinement_crop(page_pdf: Path, extract_dir: Path,
                                   recovery_dir: Path, number: int) -> Path:
    """按首次局部 OCR 的题号坐标，把整页进一步缩到当前题至下一题之前。"""
    model_path = _collection_model_json(
        extract_dir, page_pdf, reference_markdown=local_markdown)
    document = collection_recovery.load_layout_document(model_path)
    matches = [question for question in document.questions
               if question.number == number]
    if len(matches) != 1:
        raise collection_recovery.CollectionRecoveryError(
            f"整页局部 OCR 中第 {number} 题坐标检出 {len(matches)} 次，无法缩窄")
    current = matches[0]
    following = next((question for question in document.questions
                      if question.order > current.order), None)
    top = max(0.0, float(current.bbox[1]) - 0.015)
    bottom = (max(top, float(following.bbox[1]) - 0.005)
              if following is not None else 1.0)
    if bottom - top < 0.08:
        raise collection_recovery.CollectionRecoveryError(
            f"第 {number} 题缩窄区间高度不足，拒绝裁切")
    plan = collection_recovery.GapCropPlan(
        unit_title=f"choice-{number}", missing_numbers=(number,),
        previous_number=number,
        next_number=(following.number if following is not None else number + 1),
        slices=(collection_recovery.PageSlice(0, top, bottom),),
    )
    return collection_recovery.export_gap_crops(
        page_pdf, [plan], recovery_dir / "refined-source")[0].path


def _right_figure_text_column_ratio(local_markdown: str, extract_dir: Path,
                                    original_text: str) -> float | None:
    """由 MinerU 图片坐标证明“左文右图”，返回应保留的左栏宽度比例。

    仅接受同一单页至少两幅图、全部图心均在页面中线右侧、图心中位数明显偏右，
    且最右图覆盖页面右缘的布局。这个窄口径不会把普通通栏题或四图选项页误当成
    双栏；比例由最右图左边界反推，并留出 10% 页面宽的隔离带。
    """
    layout = imgorder.load_layout(extract_dir)
    if layout is None:
        return None
    boxes = []
    local_refs = _image_references(local_markdown)
    original_count = _collection_block_image_count(original_text)
    if len(local_refs) != original_count or original_count < 2:
        return None
    for _start, _end, path, _whole in local_refs:
        box = imgorder._layout_box(path, layout)
        if box is None:
            return None
        try:
            x0, _y0, x1, _y1 = (float(value) for value in box.bbox)
            page = int(box.page)
        except (AttributeError, TypeError, ValueError):
            return None
        if page != 0 or not 0 <= x0 < x1 <= 1000:
            return None
        boxes.append((x0, x1, (x0 + x1) / 2))
    if len(boxes) < 2:
        return None
    centers = sorted(item[2] for item in boxes)
    middle = len(centers) // 2
    median = (centers[middle] if len(centers) % 2
              else (centers[middle - 1] + centers[middle]) / 2)
    rightmost = max(boxes, key=lambda item: item[0])
    if (min(centers) < 550 or median < 650
            or rightmost[0] < 700 or max(item[1] for item in boxes) < 850):
        return None
    return max(0.58, min(0.68, (rightmost[0] - 100) / 1000))


def _merge_choice_options_with_original_shell(original_text: str,
                                              recovered_text: str, *,
                                              keep_images: bool) -> str:
    """采用局部 OCR 的完整 A—D，同时逐字保留整本题干和原图。

    只用于两种特殊候选：同号题被 MinerU 重复输出但仅一份选项完整，或横向裁掉
    右侧插图后得到的左栏文字。原题最后一个答题空是题干边界；候选 A—D 四元组
    是选项边界。两边任一不唯一就拒绝，绝不按选项出现顺序补标签。
    """
    import mechfix

    blanks = list(mechfix._EMPTY_ANSWER_PAREN_RE.finditer(original_text or ""))
    if not blanks:
        raise collection_recovery.CollectionRecoveryError(
            "原题没有唯一的选择题答题空，不能安全合并局部选项")
    quartet = mechfix._choice_quartet(
        recovered_text or "", known_choice=True)
    if quartet is None:
        raise collection_recovery.CollectionRecoveryError(
            "局部 MinerU 没有可唯一定位的完整 A—D 选项")
    option_text = recovered_text[quartet[0][1]:]
    labels = []
    for match in mechfix._CHOICE_LABEL_RE.finditer(option_text):
        label = next((group for group in match.groups() if group), "")
        if label:
            labels.append(label)
    if labels != list("ABCD"):
        raise collection_recovery.CollectionRecoveryError(
            "局部 MinerU 的选项标签不是唯一且顺序严格的 A—D")
    option_text = _HTML_IMG_REF_RE.sub(
        " ", _IMG_REF_RE.sub(" ", option_text))
    option_text = _strip_standalone_figure_captions(option_text).strip()
    option_text = re.sub(
        r"(?mi)^\s*图\s*(?:[1-9]\d?|[A-Da-d甲乙丙丁])"
        r"\s*[.．、:：]?\s*$", " ", option_text).strip()
    if not option_text:
        raise collection_recovery.CollectionRecoveryError(
            "局部 MinerU 的 A—D 选项正文为空")
    merged = original_text[:blanks[-1].end()].rstrip()
    merged += "\n\n" + option_text
    if keep_images:
        original_images = [whole for _start, _end, _path, whole
                           in _image_references(original_text)]
        if original_images:
            merged += "\n\n" + "\n\n".join(original_images)
    merged = mechfix.normalize_block(merged, keep_images=keep_images)
    merged = mechfix.normalize_choice_options(merged, known_choice=True)
    if not mechfix.has_complete_choice_options(merged, known_choice=True):
        raise collection_recovery.CollectionRecoveryError(
            "合并后的局部选项未形成完整 A—D")
    return merged


def _select_choice_recovery_candidate(local_markdown: str, number: int,
                                      original, *, keep_images: bool,
                                      preserve_original_shell: bool = False,
                                      ) -> tuple[str, bool]:
    """选择一个局部候选；返回正文及其图片是否仍来自局部解压目录。"""
    import mechfix

    local_markdown = mechfix.normalize_compact_choice_labels(local_markdown)
    duplicate_fallback = False
    try:
        candidate_text = collection_recovery.select_recovered_question(
            local_markdown, number, content_role="stem")
    except collection_recovery.CollectionRecoveryError as first_error:
        matches = collection_recovery._markdown_question_blocks(
            local_markdown).get(number, [])
        complete: list[str] = []
        exact_shells: list[str] = []
        original_stem = _choice_stem_signature(original.text)
        original_cjk = "".join(
            char for char in original_stem if "\u3400" <= char <= "\u9fff")
        for match in matches:
            try:
                normalized = mechfix.normalize_block(
                    match, keep_images=keep_images)
                normalized = mechfix.normalize_choice_options(
                    normalized, known_choice=True)
                collection_recovery.validate_recovered_anchor(
                    original.text, normalized, number)
                if mechfix.has_complete_choice_options(
                        normalized, known_choice=True):
                    collection_recovery._validate_recovered_block(
                        normalized, number, 20, content_role="stem")
                    complete.append(normalized)
                else:
                    candidate_stem = _choice_stem_signature(normalized)
                    candidate_cjk = "".join(
                        char for char in candidate_stem
                        if "\u3400" <= char <= "\u9fff")
                    if (candidate_stem == original_stem
                            or (len(original_cjk) >= 20
                                and candidate_cjk == original_cjk)):
                        if _visible_question_units(normalized) < 20:
                            continue
                        exact_shells.append(normalized)
            except collection_recovery.CollectionRecoveryError:
                continue
        # 重复题号本身不是放宽理由：必须同时有一份与原题干逐字一致但缺选项的
        # “壳块”，以及恰好一份锚点相符且 A—D 完整的块，才允许组合两份证据。
        if len(exact_shells) != 1 or len(complete) != 1:
            raise first_error
        candidate_text = complete[0]
        duplicate_fallback = True
    candidate_text = mechfix.normalize_block(
        candidate_text, keep_images=keep_images)
    candidate_text = mechfix.normalize_choice_options(
        candidate_text, known_choice=True)
    collection_recovery.validate_recovered_anchor(
        original.text, candidate_text, number)
    if not mechfix.has_complete_choice_options(
            candidate_text, known_choice=True):
        raise collection_recovery.CollectionRecoveryError(
            f"局部 MinerU 的第 {number} 题仍未形成完整 A—D 选项")
    if preserve_original_shell or duplicate_fallback:
        candidate_text = _merge_choice_options_with_original_shell(
            original.text, candidate_text, keep_images=keep_images)
        local_images = False
    else:
        local_images = True
    candidate = dataclasses.replace(original, text=candidate_text)
    if not _alternate_block_is_better(
            original, candidate, require_matching_stem=True):
        raise collection_recovery.CollectionRecoveryError(
            f"局部 MinerU 没有为第 {number} 题提供可证明的完整性增益")
    return candidate_text, local_images


def _recover_collection_choice_options(blocks, *, raw_path: Path,
                                       source_pdf: Path,
                                       ocr_backend: str,
                                       keep_images: bool = True,
                                       note_sink=None) -> list:
    """对整本 OCR 后仍缺选项的题做有界、可复现的单页 MinerU 重识别。

    源 PDF 文本层只负责唯一定位页；候选正文一律来自 ``force_ocr=True`` 的 MinerU
    结果。题号、题干锚点、A—D 完整性、正文和图片不缩水五道门全部通过才替换
    内存题块，任何证据不足都保留原块并让后续质量门阻断自动入库。
    """
    import mechfix

    anomalies = qualcheck.find_option_count_anomalies(blocks)
    if not anomalies or normalize_ocr_backend(ocr_backend) != OCR_MINERU:
        return blocks
    source_pdf = Path(source_pdf).resolve()
    if not source_pdf.is_file() or source_pdf.suffix.lower() != ".pdf":
        return blocks

    _ensure_src_on_path()
    cfg = _load_config_for_user("", require_mineru=True)
    source_sha = _file_sha256(source_pdf)
    recovered_blocks = list(blocks)
    for number, _labels in anomalies:
        matches = [
            (index, block) for index, block in enumerate(recovered_blocks)
            if block.zone == "stem" and block.number == number
        ]
        if not isinstance(number, int) or len(matches) != 1:
            continue
        index, original = matches[0]
        try:
            page_index = collection_recovery.locate_unique_question_page(
                source_pdf, original.text)
            key_payload = json.dumps({
                "version": _COLLECTION_RECOVERY_VERSION,
                "source_sha256": source_sha,
                "page_index": page_index,
                "number": number,
                "stem": _choice_stem_signature(original.text)[:96],
            }, ensure_ascii=False, sort_keys=True,
                separators=(",", ":")).encode("utf-8")
            recovery_key = hashlib.sha256(key_payload).hexdigest()[:24]
            recovery_dir = raw_path.parent / "choice-recovery" / recovery_key
            page_pdf = collection_recovery.export_pdf_page(
                source_pdf, page_index, recovery_dir / "source-page.pdf")
            local_raw, local_dir = _recognize_collection_recovery_crop(
                page_pdf, recovery_dir, cfg, note_sink=note_sink,
                label=f"「第 {number} 题选项」")
            attempts = [(local_raw, local_dir)]
            failures: list[str] = []
            recovered = None
            chosen_dir = None
            local_images = False
            refined_pdf = None
            refined_extract = None
            for attempt_index in range(2):
                if attempt_index == 1:
                    refined_pdf = _export_choice_refinement_crop(
                        page_pdf, local_dir, recovery_dir, number)
                    refined_dir = recovery_dir / "refined-ocr"
                    refined_raw, refined_extract = (
                        _recognize_collection_recovery_crop(
                            refined_pdf, refined_dir, cfg,
                            note_sink=note_sink,
                            label=f"「第 {number} 题选项缩窄页」"))
                    attempts.append((refined_raw, refined_extract))
                attempt_raw, attempt_dir = attempts[attempt_index]
                try:
                    candidate_text, candidate_local_images = (
                        _select_choice_recovery_candidate(
                            attempt_raw, number, original,
                            keep_images=keep_images))
                except collection_recovery.CollectionRecoveryError as exc:
                    failures.append(str(exc))
                    continue
                recovered = candidate_text
                chosen_dir = attempt_dir
                local_images = candidate_local_images
                break
            # 通栏页仍严格限制为上面的两次。只有第二次 MinerU 坐标已经证明
            # “左文右图”时，才裁去右侧图栏再识别一次文字；随后仍用原题干和原图
            # 包住候选选项，因此横裁不会造成题面内容或图片缩水。
            if (recovered is None and refined_pdf is not None
                    and refined_extract is not None):
                ratio = _right_figure_text_column_ratio(
                    refined_raw, refined_extract, original.text)
                if ratio is not None:
                    try:
                        column_pdf = collection_recovery.export_horizontal_prefix_crop(
                            refined_pdf, ratio,
                            recovery_dir / "text-column-source.pdf")
                        column_dir = recovery_dir / "text-column-ocr"
                        column_raw, column_extract = (
                            _recognize_collection_recovery_crop(
                                column_pdf, column_dir, cfg,
                                note_sink=note_sink,
                                label=f"「第 {number} 题左栏选项」"))
                        recovered, _ = _select_choice_recovery_candidate(
                            column_raw, number, original,
                            keep_images=keep_images,
                            preserve_original_shell=True)
                        chosen_dir = column_extract
                        local_images = False
                    except collection_recovery.CollectionRecoveryError as exc:
                        failures.append(str(exc))
            if recovered is None or chosen_dir is None:
                raise collection_recovery.CollectionRecoveryError(
                    failures[-1] if failures else
                    f"局部 MinerU 未恢复第 {number} 题")
            if local_images:
                recovered = collection_recovery.copy_recovery_images(
                    recovered, chosen_dir, raw_path.parent)
            recovered_blocks[index] = dataclasses.replace(
                original, text=recovered)
            if note_sink is not None:
                note_sink(
                    f"第 {number} 题整本结果缺少选项，已唯一定位原卷第 "
                    f"{page_index + 1} 页并用 MinerU 强制重识别恢复")
        except collection_recovery.CollectionRecoveryError as exc:
            logger.warning("[WARN] 合集第 %s 题选项局部恢复失败：%s", number, exc)
            if note_sink is not None:
                note_sink(qualcheck.mark_manual_review(str(exc)))
    return recovered_blocks


def convert_collection_unit_to_blocks(raw_path, *, keep_images: bool = True,
                                      num_template: str = "",
                                      boundary_mode: str = BOUNDARY_AUTO,
                                      only_numbers=None, note_sink=None,
                                      source_name: str = "合集单元",
                                      source_pdf=None,
                                      ocr_backend: str = OCR_MINERU,
                                      ocr_meta=None) -> dict:
    """已完成整本 OCR 的单元只切块，不再调用 OCR。"""
    import blockpipe

    mode = normalize_boundary_mode(boundary_mode)
    raw_path = Path(raw_path).resolve()
    if not raw_path.is_file():
        raise ConvertError(f"合集单元原文不存在: {raw_path}")
    raw_md = raw_path.read_text(encoding="utf-8")
    blocks = blockpipe.split_and_prep(
        raw_md, keep_images=keep_images, num_template=num_template,
        only_numbers=None, note_sink=note_sink,
        run_quality_checks=False, boundary_mode=mode)
    if not blocks:
        raise ConvertError(
            f"已识别出「{source_name}」的原文，但没能切出任何题目")
    if source_pdf:
        blocks = _recover_collection_choice_options(
            blocks, raw_path=raw_path, source_pdf=Path(source_pdf),
            ocr_backend=ocr_backend, keep_images=keep_images,
            note_sink=note_sink)
    # 合集允许在这一步局部重识别缺项，所以原文体检也必须延后到最终题块；否则
    # 修复成功后 notes 里仍残留旧的“必须人工校对”，免审路径会被永久阻断。
    _check_options("\n\n".join(block.text for block in blocks), note_sink)
    if note_sink is not None:
        import blocksplit
        check_numbering = mode != BOUNDARY_WHITELIST
        pairing = blocksplit.pair_blocks(
            blocks, check_number_gaps=check_numbering)
        for line in qualcheck.report(
                blocks, pairing, check_numbering=check_numbering):
            note_sink(line)
    if only_numbers:
        blocks = blockpipe._filter_by_numbers(blocks, only_numbers)
        if not blocks:
            raise ConvertError("指定的「只取题号」没有匹配到合集单元中的任何题")
    scope = raw_path.name.removesuffix("_raw.md")
    return {
        "blocks": [dataclasses.asdict(block) for block in blocks],
        "extract_dirs": [{"dir": str(raw_path.parent), "stem": scope,
                          "intercept_images": True}],
        "keep_images": keep_images,
        "source_name": source_name,
        "boundary_mode": mode,
        "ocr_backend": normalize_ocr_backend(ocr_backend),
        "ocr_meta": ocr_meta or {},
        # 合集单元在整批结束前要支持“重新转换”，所以此处收尾
        # 只拦截图片、留语料，不删 raw/images；最后一组处理后统一回收。
        "defer_cleanup": True,
    }


def convert_collection_unit(raw_path, *, include_solution: bool,
                            only_numbers=None, provider=None,
                            engine: str = ENGINE_WHOLE,
                            num_template: str = "",
                            boundary_mode: str = BOUNDARY_AUTO,
                            note_sink=None,
                            source_name: str = "合集单元",
                            ocr_backend: str = OCR_MINERU,
                            ocr_meta=None) -> str:
    """已完成 OCR 的单元直接规范化，不重复识别整本 PDF。"""
    _ensure_src_on_path()
    from src.normalizer import normalize

    mode = normalize_boundary_mode(boundary_mode)
    raw_path = Path(raw_path).resolve()
    if not raw_path.is_file():
        raise ConvertError(f"合集单元原文不存在: {raw_path}")
    raw_md = raw_path.read_text(encoding="utf-8")
    _check_options(raw_md, note_sink)
    cfg = None
    try:
        if provider is None:
            with _alpha_cwd():
                cfg = _load_config_for_user("", require_mineru=False)
        if engine == ENGINE_BLOCK:
            md = _run_block_engine(
                raw_md, cfg, provider, include_solution=include_solution,
                keep_images=True, only_numbers=only_numbers,
                artifact_dir=raw_path.parent,
                name=raw_path.name.removesuffix("_raw.md"),
                num_template=num_template, note_sink=note_sink,
                boundary_mode=mode)
        else:
            import blocksplit
            client = _make_llm_client(cfg, provider)
            llm_raw = raw_md.replace(blocksplit.SOURCE_PAGE_BREAK, "")
            md = normalize(
                llm_raw, client, include_solution=include_solution,
                keep_images=True, only_numbers=only_numbers)
        _check_preserved_image_refs(
            raw_md, md, note_sink=note_sink,
            include_solution=include_solution,
            only_numbers=only_numbers, num_template=num_template,
            boundary_mode=mode)
        scope = raw_path.name.removesuffix("_raw.md")
        (raw_path.parent / f"{scope}_normalized.md").write_text(
            md, encoding="utf-8")
        md = _intercept_images(
            md, raw_path.parent, scope, note_sink=note_sink)
        corpus.archive(
            raw_path.parent, scope,
            meta=_corpus_meta(
                cfg, engine, num_template, ocr_backend=ocr_backend,
                ocr_meta=ocr_meta or {}, boundary_mode=mode),
            texts={f"{scope}_normalized.md": md})
        return _ensure_normalized(md, Path(source_name))
    except ConvertError:
        raise
    except llm_client.LLMClientError as exc:
        raise ConvertError(str(exc)) from exc
    except Exception as exc:
        raise ConvertError(
            f"合集单元转换失败: {type(exc).__name__}: {exc}") from exc


def convert_file_to_blocks(file_path, mineru_token: str = "", *, is_image=False,
                           keep_images: bool = True, num_template: str = "",
                           boundary_mode: str = BOUNDARY_AUTO,
                           image_page_count: int = 0,
                           only_numbers=None, note_sink=None,
                           ocr_backend: str = OCR_MINERU,
                           doc2x_api_key: str = "") -> dict:
    """逐块路径「先切块、暂停等人工审核」的单文件入口。

    行为对齐 convert_file(engine=ENGINE_BLOCK) 直到切块+机械排版那一步，但
    **不跑 LLM、不清理中间产物**：extract_dir（含 images/）留在磁盘上，直到
    finish_block_review 收尾时才处理图片拦截与清理。只服务 ENGINE_BLOCK——
    ENGINE_WHOLE 的切题在 LLM 里做，没有可暂停的中间态。

    返回可直接 json 序列化的 pending dict：
        blocks        Block 列表（dataclasses.asdict）
        extract_dirs  [{"dir", "stem"}, ...]，收尾时用于图片拦截与清理
        keep_images   是否保留插图
        source_name   失败提示里要报的文件名
    """
    import blockpipe

    mode = normalize_boundary_mode(boundary_mode)
    source_path = Path(file_path).resolve()
    if not source_path.is_file():
        raise ConvertError(f"文件不存在: {source_path}")
    backend = normalize_ocr_backend(ocr_backend)
    source_stem = source_path.stem
    extract_dir = _raw_md_dir(source_stem)
    file_path = _prep_for_ocr(
        source_path, backend, force_image=is_image, work_dir=extract_dir)

    _ensure_src_on_path()
    try:
        with _alpha_cwd():
            cfg = _load_config_for_user(
                mineru_token, require_mineru=(backend == OCR_MINERU))
        raw_md, _, ocr_meta = _parse_with_ocr_backend(
            file_path, extract_dir, cfg, ocr_backend=backend,
            doc2x_api_key=doc2x_api_key, note_sink=note_sink,
            boundary_mode=mode, num_template=num_template,
            image_page_count=image_page_count)
        raw_md = _clean_mineru_text(raw_md, file_path, note_sink=note_sink)
        # 图片输入原先不落原文。改成一律落盘：审核暂停可能持续很久（快照保留 7 天），
        # 收尾时的语料留档只能从磁盘上取原文，不落盘等于图片上传永远收不到语料。
        # 落下的这份仍是临时的——finish_block_review 收尾时 _cleanup_temp 会删掉它。
        # 包在 try 里是因为原先图片分支**不写**这个文件，改成一律写就得容忍那条
        # 路径上目录可能不在的情形；写不成只影响留档，不该让转换失败。
        try:
            extract_dir.mkdir(parents=True, exist_ok=True)
            (extract_dir / f"{source_stem}_raw.md").write_text(
                raw_md, encoding="utf-8")
        except OSError as e:
            logger.warning("[WARN] 原文落盘失败（不影响转换）: %s", e)
        _ensure_raw_text(raw_md, file_path, ocr_backend=backend)
        _check_options(raw_md, note_sink)

        blocks = blockpipe.split_and_prep(
            raw_md, keep_images=keep_images, num_template=num_template,
            only_numbers=only_numbers, note_sink=note_sink,
            boundary_mode=mode)
        if not blocks:
            reason = ("指定的「只取题号」没有匹配到任何题" if only_numbers else
                      "可能是题号写法没被认出（可在「重新转换」里指定题号模板），"
                      "或这份文件其实不含题目")
            raise ConvertError(
                f"已识别出 {source_path.name} 的原文，但没能从中切出任何题目。{reason}")
        return {
            "blocks": [dataclasses.asdict(b) for b in blocks],
            "extract_dirs": [{"dir": str(extract_dir), "stem": source_stem}],
            "keep_images": keep_images,
            "source_name": source_path.name,
            "boundary_mode": mode,
            "ocr_backend": backend,
            "ocr_meta": ocr_meta,
        }
    except ConvertError:
        raise
    except Exception as e:
        raise ConvertError(f"转换失败: {type(e).__name__}: {e}") from e


def convert_exam_and_solution_to_blocks(exam_path, solution_path,
                                        mineru_token: str = "",
                                        *, keep_images: bool = True,
                                        num_template: str = "",
                                        boundary_mode: str = BOUNDARY_AUTO,
                                        exam_image_page_count: int = 0,
                                        solution_image_page_count: int = 0,
                                        only_numbers=None, note_sink=None,
                                        ocr_backend: str = OCR_MINERU,
                                        doc2x_api_key: str = "") -> dict:
    """同 convert_file_to_blocks，但输入是题干+单独解析文件（对齐
    convert_exam_and_solution 直到切块那一步）。两份各自的 extract_dir 都要
    留到收尾时处理图片拦截（与 convert_exam_and_solution 的两次
    _intercept_images 调用一致）。
    """
    import blockpipe

    mode = normalize_boundary_mode(boundary_mode)
    exam_path = Path(exam_path).resolve()
    sol_path = Path(solution_path).resolve()
    if not exam_path.is_file():
        raise ConvertError(f"题干文件不存在: {exam_path}")
    if not sol_path.is_file():
        raise ConvertError(f"解析文件不存在: {sol_path}")
    backend = normalize_ocr_backend(ocr_backend)

    _ensure_src_on_path()
    try:
        with _alpha_cwd():
            cfg = _load_config_for_user(
                mineru_token, require_mineru=(backend == OCR_MINERU))

        ((exam_dir, exam_scope),
         (sol_dir, sol_scope)) = _dual_ocr_workspaces(exam_path, sol_path)
        exam_in = _prep_for_ocr(exam_path, backend, work_dir=exam_dir)
        sol_in = _prep_for_ocr(sol_path, backend, work_dir=sol_dir)

        def _parse(p: Path, extract_dir: Path, label: str,
                   image_page_count: int):
            return _parse_with_ocr_backend(
                p, extract_dir, cfg, ocr_backend=backend,
                doc2x_api_key=doc2x_api_key,
                note_sink=note_sink, label=label,
                boundary_mode=mode, num_template=num_template,
                image_page_count=image_page_count)

        with ThreadPoolExecutor(max_workers=2) as pool:
            fut_exam = pool.submit(
                _parse, exam_in, exam_dir, "题干", exam_image_page_count)
            fut_sol = pool.submit(
                _parse, sol_in, sol_dir, "解析", solution_image_page_count)
            exam_raw, _, exam_ocr_meta = fut_exam.result()
            sol_raw, _, sol_ocr_meta = fut_sol.result()

        exam_raw = _clean_mineru_text(exam_raw, exam_path, label="题干",
                                      note_sink=note_sink)
        sol_raw = _clean_mineru_text(sol_raw, sol_path, label="解析",
                                      note_sink=note_sink)
        _ensure_raw_text(
            exam_raw, exam_path, label="题干", ocr_backend=backend)
        _check_options(exam_raw, note_sink, label="题干")
        _check_options(sol_raw, note_sink, label="解析")

        exam_raw, sol_raw = _merge_dual_image_trees(
            exam_raw, sol_raw, exam_dir, sol_dir)

        combined = (exam_raw.rstrip()
                    + "\n\n# 参考答案与解析\n\n"
                    + sol_raw.lstrip())
        try:
            exam_dir.mkdir(parents=True, exist_ok=True)
            (exam_dir / f"{exam_scope}_combined_raw.md").write_text(
                combined, encoding="utf-8")
        except OSError as e:
            logger.warning("[WARN] 合并原文落盘失败（不影响转换）: %s", e)

        blocks = blockpipe.split_and_prep(
            combined, keep_images=keep_images, num_template=num_template,
            only_numbers=only_numbers, note_sink=note_sink,
            boundary_mode=mode)
        if not blocks:
            reason = ("指定的「只取题号」没有匹配到任何题" if only_numbers else
                      "可能是题号写法没被认出（可在「重新转换」里指定题号模板），"
                      "或这份文件其实不含题目")
            raise ConvertError(
                f"已识别出 {exam_path.name} 的原文，但没能从中切出任何题目。{reason}")
        return {
            "blocks": [dataclasses.asdict(b) for b in blocks],
            "extract_dirs": [
                {"dir": str(exam_dir), "stem": exam_scope,
                 "intercept_images": True},
                {"dir": str(sol_dir), "stem": sol_scope,
                 "intercept_images": False},
            ],
            "keep_images": keep_images,
            "source_name": exam_path.name,
            "boundary_mode": mode,
            "ocr_backend": backend,
            "ocr_meta": {"exam": exam_ocr_meta, "solution": sol_ocr_meta},
        }
    except ConvertError:
        raise
    except Exception as e:
        raise ConvertError(f"转换失败: {type(e).__name__}: {e}") from e


def finish_block_review(pending: dict, *, action: str, include_solution: bool,
                        provider=None, note_sink=None) -> str:
    """人工审核块之后的收尾：送入 AI 标准化（action="ai"）或跳过 AI 直接渲染
    （action="skip"），再补上暂停时推迟的两件事——图片拦截与中间产物清理
    （见 convert_file_to_blocks 的说明），最后校验产出非空。

    provider 为 None 且 action="ai" 时才需要现读一次 project-alpha 的集中
    DeepSeek 配置（老回落行为）；action="skip" 或已给 provider 都用不上 cfg，
    不为它多做一次 _alpha_cwd 切换。
    """
    import blocksplit
    import blockpipe

    _ensure_src_on_path()
    blocks = [blocksplit.Block(**d) for d in pending["blocks"]]
    keep_images = bool(pending.get("keep_images", True))
    mode = normalize_boundary_mode(pending.get("boundary_mode"))

    if action == "ai":
        cfg = None
        if provider is None:
            with _alpha_cwd():
                cfg = _load_llm_fallback_cfg()
        client = _make_llm_client(cfg, provider)
        md = blockpipe.normalize_and_render(
            blocks, client, keep_images=keep_images,
            include_solution=include_solution, boundary_mode=mode)
    else:
        md = blockpipe.render_without_ai(
            blocks, include_solution=include_solution, boundary_mode=mode)

    if keep_images:
        _check_preserved_image_refs(
            "\n\n".join(block.text for block in blocks), md,
            note_sink=note_sink, include_solution=include_solution,
            blocks=blocks, boundary_mode=mode)

    from src.pipeline import _cleanup_temp

    for ed in pending.get("extract_dirs") or []:
        extract_dir = Path(ed["dir"])
        stem = ed["stem"]
        if keep_images and ed.get("intercept_images", True):
            md = _intercept_images(
                md, extract_dir, stem, note_sink=note_sink)
        # 留档排在清理之前（同 _convert_pdf）。这条路径没有 cfg 可问版本——暂停点
        # 之后 provider/cfg 不一定在手，engine 则是确定的：能走到人工审核就是逐块。
        corpus.archive(extract_dir, stem,
                       meta={"engine": ENGINE_BLOCK, "review": action,
                             "boundary_mode": mode,
                             "ocr_backend": pending.get("ocr_backend", OCR_MINERU),
                             "ocr_meta": pending.get("ocr_meta") or {}},
                       texts={f"{stem}_normalized.md": md})
        if not pending.get("defer_cleanup"):
            try:
                _cleanup_temp(extract_dir, stem, keep_images=keep_images)
            except Exception as e:
                logger.warning("[WARN] 审核后清理中间产物失败（不影响转换）: %s", e)

    source = Path(pending.get("source_name") or "文件")
    return _ensure_normalized(md, source)


def _run_block_engine(raw_md: str, cfg, provider, *, include_solution: bool,
                      keep_images: bool, only_numbers, artifact_dir,
                      name: str, num_template: str = "",
                      note_sink=None,
                      boundary_mode: str = BOUNDARY_AUTO) -> str:
    """逐块路径入口。放在这里而不是 blockpipe 里，是为了让 LLM 客户端的构造
    方式（provider / 回落 DeepSeek）与老路径完全一致，两条路径共用同一套配置。
    """
    import blockpipe

    client = _make_llm_client(cfg, provider)
    return blockpipe.run(raw_md, client, keep_images=keep_images,
                         include_solution=include_solution,
                         only_numbers=only_numbers,
                         artifact_dir=artifact_dir, name=name,
                         num_template=num_template, note_sink=note_sink,
                         boundary_mode=normalize_boundary_mode(boundary_mode))


def convert_file(file_path, mineru_token: str = "", *, is_image=False,
                 include_solution=False, keep_images=True,
                 only_numbers=None, provider=None,
                 engine: str = ENGINE_WHOLE, num_template: str = "",
                 boundary_mode: str = BOUNDARY_AUTO,
                 image_page_count: int = 0,
                 note_sink=None, ocr_backend: str = OCR_MINERU,
                 doc2x_api_key: str = "") -> str:
    """把一个 PDF/图片文件转换为规范化 md 文本。

    file_path: 待转换文件的绝对路径。
    mineru_token: 当前登录用户自己的 MinerU token（明文，调用前已由
        app.py 用 crypto_utils.decrypt_token 解密）。
    is_image: 图片走 MinerU 直传（预留点①）。
    include_solution: 是否同时规范化解析（预留点②，透传给 run_parse）。
    keep_images: 是否保留题目插图（默认 True）。保留时把 MinerU 解析出的图
        拷到 config.IMAGES_DIR/<scope>/，md 里路径改写为 /qimages/<scope>/<file>。
    only_numbers: 仅导入指定题号的题（如 [8,11,14,18,19] 压轴题）。None=全部。
        MinerU 仍会 OCR 整份 PDF（它不认题号），但 DeepSeek 只规范化选中题，
        输出更短、更快、更省额度。
    provider: 规范化用的 LLM 配置（llm_provider.ProviderConfig）。None=用
        project-alpha .env 里的集中 DeepSeek（老行为）。
    engine: ENGINE_WHOLE（默认，老的整篇规范化）或 ENGINE_BLOCK（先机械切块
        再逐块判定）。输出格式两者相同，下游无差别。
    num_template: 题号模板（`x.` `第X题` 之类，语法见 blocksplit.compile_dialect），
        空串=自动判定。**只对 ENGINE_BLOCK 有效**：整篇路径的切题在 LLM 里做，
        没有代码层的题号正则可钉。模板非法会抛 ConvertError。
    note_sink: 单参可调用对象，切块阶段有话对用户说时调一次（见 blockpipe.run）。
    返回规范化 md 文本。失败抛 ConvertError。
    """
    source_path = Path(file_path).resolve()
    if not source_path.is_file():
        raise ConvertError(f"文件不存在: {source_path}")
    mode = normalize_boundary_mode(boundary_mode)
    backend = normalize_ocr_backend(ocr_backend)
    is_image_input = is_image or is_image_file(source_path.name)
    source_stem = source_path.stem
    extract_dir = _raw_md_dir(source_stem)
    file_path = _prep_for_ocr(
        source_path, backend, force_image=is_image, work_dir=extract_dir)
    # Doc2X 只有 PDF 链路；图片已由 _prep_for_ocr 合成单页 PDF。
    if backend == OCR_DOC2X:
        is_image_input = False
    elif file_path.suffix.lower() == ".pdf":
        is_image_input = False

    _ensure_src_on_path()
    try:
        # normalizer 的 prompt 模板按其自身 __file__ 定位，不依赖 CWD；
        # 中间产物一律走 _raw_md_dir() 的绝对路径。故这里不再整段切 CWD，
        # 只有读配置那一小步进 _alpha_cwd()（见该函数注释）。
        with _alpha_cwd():
            cfg = _load_config_for_user(
                mineru_token, require_mineru=(backend == OCR_MINERU))

        if is_image_input:
            return _convert_image(file_path, cfg, keep_images, only_numbers,
                                  provider, engine, num_template, note_sink,
                                  boundary_mode=mode,
                                  image_page_count=image_page_count,
                                  extract_dir=extract_dir,
                                  source_path=source_path)
        return _convert_pdf(file_path, cfg, include_solution, keep_images,
                            only_numbers, provider, engine, num_template,
                            note_sink, backend, doc2x_api_key,
                            boundary_mode=mode,
                            image_page_count=image_page_count,
                            extract_dir=extract_dir,
                            source_path=source_path)
    except ConvertError:
        raise
    except llm_client.LLMClientError as e:
        # LLM 那边的报错本身已经写清了该怎么办（比如调大 max_tokens），
        # 别再套一层「转换失败: XxxError:」把它埋掉。
        raise ConvertError(str(e)) from e
    except Exception as e:
        raise ConvertError(f"转换失败: {type(e).__name__}: {e}") from e


def _convert_pdf(file_path: Path, cfg, include_solution: bool,
                 keep_images: bool = True, only_numbers=None,
                 provider=None, engine: str = ENGINE_WHOLE,
                 num_template: str = "", note_sink=None,
                 ocr_backend: str = OCR_MINERU,
                 doc2x_api_key: str = "", *,
                 boundary_mode: str = BOUNDARY_AUTO,
                 image_page_count: int = 0,
                 extract_dir: Path | None = None,
                 source_path: Path | None = None) -> str:
    """PDF/Word：MinerU 拿原文 → LLM 规范化，与 _convert_image 同一套编排。

    原先是调 project-alpha 的 run_parse，但它内部自己 new DeepSeekClient
    （pipeline.py 里那句），外面换不掉 LLM，所以改为在这里自编排。行为对齐了
    run_parse 中 QuizForge 实际会走到的部分：
      - run_parse 的「试卷/题集」模式判定对本项目的上传路径恒为「题集」，
        答案文件跳过、试卷来源注入都不会触发，故省略；
      - validate/save_report 只写报告和日志，导入链不消费（图片路径本来也不走
        它），故省略；
      - 中间文件清理直接复用 run_parse 用的那个 _cleanup_temp，保持磁盘遗留
        行为完全一致（keep_images 时保留 images/ 供 _intercept_images 拷图）。
    """
    from src.normalizer import normalize
    from src.pipeline import _cleanup_temp

    # 预处理后的 PDF 名会带 `_word_input` 等后缀；产物仍必须归到原文件的工作区，
    # 否则同一次任务会拆成两个目录，预处理件也无法随 OCR 发布/清理一起收走。
    source_path = Path(source_path) if source_path is not None else file_path
    source_stem = source_path.stem
    mode = normalize_boundary_mode(boundary_mode)
    extract_dir = (Path(extract_dir) if extract_dir is not None
                   else _raw_md_dir(source_stem))
    raw_md, _, ocr_meta = _parse_with_ocr_backend(
        file_path, extract_dir, cfg, ocr_backend=ocr_backend,
        doc2x_api_key=doc2x_api_key, note_sink=note_sink,
        boundary_mode=mode, num_template=num_template,
        image_page_count=image_page_count)
    (extract_dir / f"{source_stem}_raw.md").write_text(raw_md, encoding="utf-8")
    # 落盘留的是 MinerU 未清洗的原文（诊断用，要看得出乱码本来的样子）；
    # 往下走的这份才清洗，不能让乱码字节混进 LLM 输入
    raw_md = _clean_mineru_text(raw_md, source_path, note_sink=note_sink)
    # 原文落盘之后、烧 LLM 额度之前判空：坏 PDF 的诊断线索还留在磁盘上
    _ensure_raw_text(raw_md, source_path, ocr_backend=ocr_backend)
    _check_options(raw_md, note_sink)

    if engine == ENGINE_BLOCK:
        # 逐块路径：include_solution 默认给 True——切块阶段本来就要判「哪段是
        # 解析」，识别出来再决定要不要输出，比让模型装作没看见更稳。
        md = _run_block_engine(
            raw_md, cfg, provider, include_solution=include_solution,
            keep_images=keep_images, only_numbers=only_numbers,
            artifact_dir=extract_dir, name=source_stem,
            num_template=num_template, note_sink=note_sink,
            boundary_mode=mode)
    else:
        import blocksplit
        client = _make_llm_client(cfg, provider)
        # 页界标记只供机械白名单切分器消费，整篇 LLM 路径绝不能看到内部协议。
        llm_raw = raw_md.replace(blocksplit.SOURCE_PAGE_BREAK, "")
        md = normalize(llm_raw, client, include_solution=include_solution,
                       keep_images=keep_images, only_numbers=only_numbers)
    if keep_images:
        _check_preserved_image_refs(
            raw_md, md, note_sink=note_sink,
            include_solution=include_solution, only_numbers=only_numbers,
            num_template=num_template, boundary_mode=mode)
    (extract_dir / f"{source_stem}_normalized.md").write_text(
        md, encoding="utf-8")

    if keep_images:
        md = _intercept_images(
            md, extract_dir, source_stem, note_sink=note_sink)
    # 留档必须排在清理之前：_cleanup_temp 的保留清单里没有 _raw.md / _blocks.json
    corpus.archive(extract_dir, source_stem,
                   meta=_corpus_meta(
                       cfg, engine, num_template, ocr_backend=ocr_backend,
                       ocr_meta=ocr_meta, boundary_mode=mode))
    _cleanup_temp(extract_dir, source_stem, keep_images=keep_images)
    return _ensure_normalized(md, source_path)


def convert_exam_and_solution(exam_path, solution_path, mineru_token: str = "",
                              only_numbers=None, provider=None,
                              engine: str = ENGINE_WHOLE,
                              num_template: str = "",
                              boundary_mode: str = BOUNDARY_AUTO,
                              exam_image_page_count: int = 0,
                              solution_image_page_count: int = 0,
                              note_sink=None,
                              ocr_backend: str = OCR_MINERU,
                              doc2x_api_key: str = "") -> str:
    """题干文件 + 单独的解析/答案文件 → 合并后一次规范化，按题号关联解析。

    两份各自过 MinerU 拿原文，把解析原文接到题干原文后面（标为「参考答案与解析」
    区块），再交给 DeepSeek 以 include_solution=True 规范化。DeepSeek 看全文、
    按题号把卷末答案归到各题（见 normalizer 强化的关联规则）。
    """
    exam_path = Path(exam_path).resolve()
    sol_path = Path(solution_path).resolve()
    if not exam_path.is_file():
        raise ConvertError(f"题干文件不存在: {exam_path}")
    if not sol_path.is_file():
        raise ConvertError(f"解析文件不存在: {sol_path}")
    mode = normalize_boundary_mode(boundary_mode)
    backend = normalize_ocr_backend(ocr_backend)

    _ensure_src_on_path()
    try:
        from src.normalizer import normalize

        with _alpha_cwd():
            cfg = _load_config_for_user(
                mineru_token, require_mineru=(backend == OCR_MINERU))

        # docx→pdf / 图片→PDF 的预处理先串行做完（转换器单实例，并发会冲突）
        ((exam_dir, exam_scope),
         (sol_dir, sol_scope)) = _dual_ocr_workspaces(exam_path, sol_path)
        exam_in = _prep_for_ocr(exam_path, backend, work_dir=exam_dir)
        sol_in = _prep_for_ocr(sol_path, backend, work_dir=sol_dir)

        # 两次 MinerU 相互独立、且是纯 I/O（上传/轮询/下载，不占 GIL），
        # 并行跑把墙钟时间砍掉近一半，两边的轮询等待也重叠。
        def _parse(p: Path, extract_dir: Path, label: str,
                   image_page_count: int):
            return _parse_with_ocr_backend(
                p, extract_dir, cfg, ocr_backend=backend,
                doc2x_api_key=doc2x_api_key,
                note_sink=note_sink, label=label,
                boundary_mode=mode, num_template=num_template,
                image_page_count=image_page_count)

        with ThreadPoolExecutor(max_workers=2) as pool:
            fut_exam = pool.submit(
                _parse, exam_in, exam_dir, "题干", exam_image_page_count)
            fut_sol = pool.submit(
                _parse, sol_in, sol_dir, "解析", solution_image_page_count)
            exam_raw, _, exam_ocr_meta = fut_exam.result()  # 线程内异常在此重新抛出
            sol_raw, _, sol_ocr_meta = fut_sol.result()

        exam_raw = _clean_mineru_text(exam_raw, exam_path, label="题干",
                                      note_sink=note_sink)
        sol_raw = _clean_mineru_text(sol_raw, sol_path, label="解析",
                                     note_sink=note_sink)
        # 两份都查：选项主要在题干里，但解析文件常把选项重抄一遍，那份丢了同样
        # 会让 LLM 关联错。加 label 是因为两份的题号会重复，不说清是哪份没法对照。
        # 题干那份没识别出字就没有卷子可导；解析那份空了则只是没有解析可关联，
        # 题干仍然值得转，所以只对题干判空。
        _ensure_raw_text(
            exam_raw, exam_path, label="题干", ocr_backend=backend)
        _check_options(exam_raw, note_sink, label="题干")
        _check_options(sol_raw, note_sink, label="解析")

        exam_raw, sol_raw = _merge_dual_image_trees(
            exam_raw, sol_raw, exam_dir, sol_dir)

        # 拼接：题干原文 + 卷末答案区，交给 DeepSeek 按题号关联
        combined = (exam_raw.rstrip()
                    + "\n\n# 参考答案与解析\n\n"
                    + sol_raw.lstrip())
        # 拼好的原文落盘：这条路径的两份 MinerU 产物各自存了，合并后的没存过，
        # 而切块与配对都是在合并结果上做的，出问题只看两份分开的原文对不上账。
        try:
            exam_dir.mkdir(parents=True, exist_ok=True)
            (exam_dir / f"{exam_scope}_combined_raw.md").write_text(
                combined, encoding="utf-8")
        except OSError as e:
            logger.warning("[WARN] 合并原文落盘失败（不影响转换）: %s", e)

        if engine == ENGINE_BLOCK:
            # 那行 `# 参考答案与解析` 正好是 blocksplit 判解析区最强的那个信号，
            # 这条路径的题干区/解析区边界因此是确定的，不依赖任何启发式。
            md = _run_block_engine(
                combined, cfg, provider, include_solution=True,
                keep_images=True, only_numbers=only_numbers,
                artifact_dir=exam_dir,
                name=exam_scope + "_combined",
                num_template=num_template, note_sink=note_sink,
                boundary_mode=mode)
        else:
            import blocksplit
            client = _make_llm_client(cfg, provider)
            llm_raw = combined.replace(blocksplit.SOURCE_PAGE_BREAK, "")
            md = normalize(llm_raw, client, include_solution=True,
                           keep_images=True, only_numbers=only_numbers)
        _check_preserved_image_refs(
            combined, md, note_sink=note_sink, include_solution=True,
            only_numbers=only_numbers, num_template=num_template,
            boundary_mode=mode)
        # 题干与解析各自的图都解压在 <project-alpha>/output/raw_md/<stem>/images/
        # 下。两个 extract_dir 都要扫。
        md = _intercept_images(
            md, exam_dir, exam_scope,
            note_sink=note_sink)
        # 这条路径不调 _cleanup_temp，产物暂时还在磁盘上；仍然留档，理由是位置：
        # raw_md/<stem>/ 会被下一次同名上传覆盖，也不带版本与引擎归因。切块吃的是
        # combined，所以只留题干那个目录（sol 那份的原文已经并进 combined 了）。
        corpus.archive(exam_dir, exam_scope,
                       meta=_corpus_meta(
                           cfg, engine, num_template, ocr_backend=backend,
                           ocr_meta={"exam": exam_ocr_meta,
                                     "solution": sol_ocr_meta},
                           boundary_mode=mode),
                       texts={f"{exam_scope}_normalized.md": md})
        return _ensure_normalized(md, exam_path)
    except ConvertError:
        raise
    except llm_client.LLMClientError as e:
        raise ConvertError(str(e)) from e
    except Exception as e:
        raise ConvertError(f"转换失败: {type(e).__name__}: {e}") from e


def _prep_for_mineru(path: Path, work_dir: Path) -> Path:
    """把输入准备成 MinerU 能吃的文件：.docx 先转 PDF，大图片也转 PDF
    （绕开 MinerU 图片直传的 10MB 限制），其余直接用。

    用本文件的 _docx_to_pdf（pandoc+xelatex，跨平台），不用 project-alpha 的
    _ensure_pdf（Windows Word COM，线上 Ubuntu 服务器跑不了）。
    """
    if is_word_file(path.name):
        return _docx_to_pdf(path, work_dir)
    if is_image_file(path.name):
        pdf_path = _oversized_image_to_pdf(path, work_dir)
        if pdf_path is not None:
            return pdf_path
    return path


def _prep_for_ocr(path: Path, ocr_backend: str, *, force_image=False,
                  work_dir: Path | None = None) -> Path:
    """把 Word/图片整理为所选 OCR 服务能接收的输入。

    Doc2X 的本链路只接 PDF，所以图片无论大小都合成单页 PDF；MinerU 只转换超过
    直传限制的大图。两者生成文件都放在该 stem 的中间产物目录，不在输入文件旁
    留未登记临时件，更不能覆盖题库里同 stem 的真实 PDF。
    """
    path = Path(path)
    work_dir = Path(work_dir) if work_dir is not None else _raw_md_dir(path.stem)
    if is_word_file(path.name):
        return _docx_to_pdf(path, work_dir)
    image_input = force_image or is_image_file(path.name)
    if normalize_ocr_backend(ocr_backend) == OCR_DOC2X and image_input:
        if not is_image_file(path.name):
            raise ConvertError("平台 OCR 图片输入的文件扩展名不受支持")
        work_dir.mkdir(parents=True, exist_ok=True)
        return images_to_pdf(
            [path], work_dir / f"{path.stem}_cloud_input.pdf")
    return _prep_for_mineru(path, work_dir)


def _convert_image(file_path: Path, cfg, keep_images: bool = True,
                   only_numbers=None, provider=None,
                   engine: str = ENGINE_WHOLE, num_template: str = "",
                   note_sink=None, *,
                   boundary_mode: str = BOUNDARY_AUTO,
                   image_page_count: int = 0,
                   extract_dir: Path | None = None,
                   source_path: Path | None = None) -> str:
    """图片（预留点①）：绕过 run_parse 的白名单，直接 MinerU + normalize。"""
    from src.mineru_client import MineruClient
    from src.normalizer import normalize

    source_path = Path(source_path) if source_path is not None else file_path
    source_stem = source_path.stem
    mode = normalize_boundary_mode(boundary_mode)
    extract_dir = (Path(extract_dir) if extract_dir is not None
                   else _raw_md_dir(source_stem))
    raw_md, _ = ocr_pool.run(
        OCR_MINERU,
        lambda token: _parse_mineru_with_ocr_retry(
            MineruClient(token, cfg.mineru_model_version), file_path,
            extract_dir, note_sink=note_sink, boundary_mode=mode,
            num_template=num_template),
        fallback=cfg.mineru_token)
    raw_md, page_meta = _apply_image_page_boundaries(
        raw_md, extract_dir, boundary_mode=mode,
        image_page_count=image_page_count, ocr_backend=OCR_MINERU,
        note_sink=note_sink)
    raw_md = _clean_mineru_text(raw_md, source_path, note_sink=note_sink)
    raw_md = _repair_choice_images(raw_md, extract_dir, note_sink)
    _ensure_raw_text(raw_md, source_path)
    _check_options(raw_md, note_sink)
    if engine == ENGINE_BLOCK:
        md = _run_block_engine(
            raw_md, cfg, provider, include_solution=False,
            keep_images=keep_images, only_numbers=only_numbers,
            artifact_dir=extract_dir, name=source_stem,
            num_template=num_template, note_sink=note_sink,
            boundary_mode=mode)
    else:
        import blocksplit
        client = _make_llm_client(cfg, provider)
        llm_raw = raw_md.replace(blocksplit.SOURCE_PAGE_BREAK, "")
        md = normalize(llm_raw, client, include_solution=False,
                       keep_images=keep_images, only_numbers=only_numbers)
    if keep_images:
        _check_preserved_image_refs(
            raw_md, md, note_sink=note_sink, include_solution=False,
            only_numbers=only_numbers, num_template=num_template,
            boundary_mode=mode)
        md = _intercept_images(
            md, extract_dir, source_stem, note_sink=note_sink)
    # 图片这条路径**从不把原文落盘**（也不调 _cleanup_temp），所以 raw_md 只能
    # 从内存里给。照片/截图是 OCR 最易出错的一类输入，正是最该攒的语料。
    corpus.archive(extract_dir, source_stem,
                   meta=_corpus_meta(
                       cfg, engine, num_template, ocr_meta=page_meta,
                       boundary_mode=mode),
                   texts={f"{source_stem}_raw.md": raw_md,
                          f"{source_stem}_normalized.md": md})
    return _ensure_normalized(md, source_path)


# 匹配 md 图片引用：![alt](images/xxx.jpg) —— 只认相对 images/ 路径（MinerU 输出格式）
_IMG_REF_RE = re.compile(
    r"!\[([^\]]*)\]\(\s*<?(?:\./)?images/([^)\s>]+)>?\s*\)")
_HTML_IMG_REF_RE = re.compile(
    r"<img\b[^>]*\bsrc\s*=\s*['\"](?:\./)?images/([^'\"]+)['\"][^>]*>",
    re.IGNORECASE,
)


def _image_ref_counter(text: str):
    """按路径统计 Markdown 图片引用；同一张图出现两次也必须守恒。"""
    from collections import Counter

    value = text or ""
    refs = Counter(match.group(2) for match in _IMG_REF_RE.finditer(value))
    refs.update(match.group(1) for match in _HTML_IMG_REF_RE.finditer(value))
    return refs


def _expected_image_refs(raw_md: str, *, include_solution: bool,
                         only_numbers=None, num_template: str = "",
                         blocks=None,
                         boundary_mode: str = BOUNDARY_AUTO):
    """算出本次输出范围内理应保留的图片引用；无法可靠切题时返回 ``None``。

    全量且带解析时无需判断归属，原文引用全部守恒。其余情形要排除未选题号或答案区
    中本来就不要求输出的图片，避免把“按用户要求省略”误报成模型丢图。
    """
    mode = normalize_boundary_mode(boundary_mode)
    provided_blocks = blocks is not None
    if blocks is None and include_solution and not only_numbers:
        return _image_ref_counter(raw_md)

    if blocks is None:
        try:
            import blocksplit

            blocks = blocksplit.split_blocks(
                raw_md, num_template=num_template, boundary_mode=mode)
        except Exception as exc:
            logger.warning("[WARN] 图片引用守恒检查无法切题，已跳过: %s", exc)
            return None
    if not blocks:
        return None

    wanted = set(only_numbers or [])
    stems = [block for block in blocks if block.zone == "stem"]
    if provided_blocks and not stems:
        # 人工审核后的 Block 可能因用户调整而暂时没有可靠 zone；此时至少核对所有
        # 仍保留在审核内容中的引用，不能因为结构元数据不完整而跳过守恒门。
        return _image_ref_counter(
            "\n\n".join(block.text for block in blocks))
    if wanted:
        selected_stems = [block for block in stems if block.number in wanted]
        # 题号方言没识别出来时不能拿全卷图片去要求“只取题号”的输出保留。
        if not selected_stems:
            return None
    else:
        selected_stems = stems

    selected = list(selected_stems)
    if include_solution:
        try:
            import blocksplit

            paired = blocksplit.pair_blocks(
                blocks, check_number_gaps=(mode != BOUNDARY_WHITELIST))
            selected = []
            for stem, solution in paired.paired:
                if wanted and stem.number not in wanted:
                    continue
                selected.append(stem)
                if solution is not None:
                    selected.append(solution)
        except Exception as exc:
            logger.warning("[WARN] 图片引用守恒检查无法配对解析，保守只核对题干: %s", exc)

    return _image_ref_counter("\n\n".join(block.text for block in selected))


def _check_preserved_image_refs(raw_md: str, normalized_md: str, *,
                                note_sink=None, include_solution: bool,
                                only_numbers=None, num_template: str = "",
                                blocks=None,
                                boundary_mode: str = BOUNDARY_AUTO) -> None:
    """阻止 OCR 原文中的题图被规范化模型静默删掉。

    不自动把引用塞回结果：脱离题块归属后盲目补图可能把 A 题的图挂到 B 题。这里只
    记强制校对并让免审门暂停，用户可在校对页对照原文处理。
    """
    expected = _expected_image_refs(
        raw_md, include_solution=include_solution,
        only_numbers=only_numbers, num_template=num_template, blocks=blocks,
        boundary_mode=boundary_mode)
    if not expected:
        return
    missing = expected - _image_ref_counter(normalized_md)
    if not missing:
        return
    details = "、".join(
        f"{name}（{count} 处）" if count > 1 else name
        for name, count in list(missing.items())[:5])
    if len(missing) > 5:
        details += f"等 {len(missing)} 个文件"
    note = qualcheck.mark_manual_review(
        f"规范化结果少了原文中的图片引用：{details}；已暂停免审入库，"
        "请在校对页对照原文件补图")
    logger.warning("[WARN] %s", note)
    if note_sink is not None:
        note_sink(note)


def _intercept_images(md_text: str, extract_dir: Path, scope: str, *,
                      note_sink=None) -> str:
    """把 md 里 `![](images/xxx)` 的图从 extract_dir/images/ 拷到 ASSETS_DIR，
    并把引用改写为 Obsidian 双链嵌入 `![[<scope>_<file>]]`。

    单机版与服务器版的两处差异：
    - **扁平存放**：服务器版按 scope 建子目录（`IMAGES_DIR/<scope>/<file>`），
      这里全部平铺在 `_assets/` 下，因为 Obsidian 的 `![[文件名]]` 双链是按
      vault 内全局文件名解析的，放进子目录双链就失效了。同名冲突改用文件名
      前缀（`<scope>_<file>`）来避，scope 仍是来源文件的 stem。
    - **双链语法**：`![[...]]` 而不是 `![](...)`，这样同一份 md 在 Obsidian 里
      直接就能渲染出图 —— 这是做插件的前提。

    图缺失时保留原引用，并写入强制校对提示；这样用户仍能抢救文字，但批量免审
    不会把带断图的题直接写进题库。
    返回改写后的 md。extract_dir 相对/绝对均可（相对则以当前 CWD 解析）。
    """
    import hashlib
    import tempfile

    from PIL import Image

    extract_dir = Path(extract_dir)
    dest_dir = config.ASSETS_DIR
    # Doc2X 偶尔保留 HTML 图片标签。先归一为本链路的 Markdown 引用，随后与普通
    # 图片共用完整性校验、内容寻址和 Obsidian 双链改写，不能让 HTML 形态绕过拦截。
    md_text = _HTML_IMG_REF_RE.sub(
        lambda match: f"![](images/{match.group(1)})", md_text)
    refs = list(_IMG_REF_RE.finditer(md_text))
    if not refs:
        return md_text

    dest_dir.mkdir(parents=True, exist_ok=True)
    copied = 0
    # scope 进文件名而不是进目录：双链按 vault 内全局文件名解析，不能有子目录。
    prefix = re.sub(r"[^\w.-]", "_", scope or "img")

    def _replace(m: "re.Match") -> str:
        nonlocal copied
        fname = m.group(2)
        # 防目录穿越：只取文件名部分
        safe_name = Path(fname).name
        src = extract_dir / "images" / safe_name
        if not src.is_file():
            logger.warning("[WARN] 图片缺失，保留原引用: %s", src)
            if note_sink is not None:
                note_sink(qualcheck.mark_manual_review(
                    f"识别结果引用的图片 {safe_name} 不存在，已保留原引用；"
                    "请在校对页对照原文件补图"))
            return m.group(0)
        try:
            # 上游 ZIP 内的文件可能存在但已经截断/伪装扩展名；只看 is_file 会让题目
            # 引用一个永远打不开的资产。完整解码校验不改变像素，也不会二次压缩。
            with Image.open(src) as image:
                image.verify()
        except Exception as e:
            logger.warning("[WARN] OCR 图片已损坏，保留原引用 %s: %s", src, e)
            if note_sink is not None:
                note_sink(qualcheck.mark_manual_review(
                    f"识别结果中的图片 {safe_name} 已损坏或格式无效，已保留原引用；"
                    "请在校对页对照原文件补图"))
            return m.group(0)

        digest = hashlib.sha256()
        try:
            with src.open("rb") as source_file:
                for chunk in iter(lambda: source_file.read(1024 * 1024), b""):
                    digest.update(chunk)
            # 内容寻址后，同一来源反复重转不会覆盖旧题仍在引用的资产；相同图片则自然
            # 复用同一个文件。保留原名尾部便于人在 _assets 中辨认。
            digest_hex = digest.hexdigest()
            dest = None
            # 常规文件名只加 16 位摘要，避免 Windows 深层题库触及 260 字符路径限制；
            # 真遇到摘要前缀冲突就逐级加长，而不是覆盖旧资产。
            for digest_length in (16, 24, 32, 64):
                candidate_name = (
                    f"{prefix}_{digest_hex[:digest_length]}_{safe_name}")
                candidate = dest_dir / candidate_name
                if not candidate.exists():
                    dest = candidate
                    break
                existing_digest = hashlib.sha256()
                with candidate.open("rb") as existing_file:
                    for chunk in iter(
                            lambda: existing_file.read(1024 * 1024), b""):
                        existing_digest.update(chunk)
                if existing_digest.digest() == digest.digest():
                    dest = candidate
                    break
            if dest is None:
                raise OSError(f"图片内容摘要冲突：{safe_name}")
            dest_name = dest.name
            if not dest.exists():
                temp_path = None
                try:
                    with tempfile.NamedTemporaryFile(
                            prefix=f".{dest_name}.", suffix=".tmp",
                            dir=dest_dir, delete=False) as temp_file:
                        temp_path = Path(temp_file.name)
                        with src.open("rb") as source_file:
                            shutil.copyfileobj(source_file, temp_file,
                                               length=1024 * 1024)
                    os.replace(temp_path, dest)
                finally:
                    if temp_path is not None:
                        temp_path.unlink(missing_ok=True)
                copied += 1
        except OSError as e:
            logger.warning("[WARN] 拷贝图片失败 %s: %s", src, e)
            if note_sink is not None:
                note_sink(qualcheck.mark_manual_review(
                    f"图片 {safe_name} 保存失败，已保留原引用；"
                    "请在校对页重试或补图"))
            return m.group(0)
        # 丢掉 alt：Obsidian 双链的 | 后缀是显示宽度而非 alt，写进去会被当宽度解析
        return f"![[{dest_name}]]"

    new_md = _IMG_REF_RE.sub(_replace, md_text)
    logger.info("[OK] 拦截图片 %d 张 -> %s", copied, dest_dir)
    return new_md
