"""转换接口层：上传的 PDF/图片 → 内置 vendor/project_alpha 的 MinerU+DeepSeek → 规范化 md。

project_alpha 是 vendor 进本项目的内部包（`vendor/project_alpha/`），不依赖任何
外部路径；本文件仍保留 sys.path 注入 + 临时切 CWD 的方式来读取它（沿用其
`src.xxx` 相对导入与 dotenv 的 CWD 探测），全程持锁（`_alpha_cwd()`）。
中间产物路径一律**绝对化**（`_RAW_MD_ROOT` / `_raw_md_dir()`）。

预留扩展点（见 convert_file 参数）：
  ① is_image=True       —— 图片输入（绕过 run_parse 的 pdf/docx 白名单）
  ② include_solution    —— 带解析（answer/solution）一起规范化

双文件入口：
  convert_exam_and_solution(exam, solution) —— 题干与解析分属两个文件时，
  各自 OCR 后拼接，交给 DeepSeek 按题号关联解析（见 normalizer 关联规则）。

两个可切换的维度（默认值都保持本功能上线前的老行为）：
  - engine：ENGINE_WHOLE 整篇规范化（老） / ENGINE_BLOCK 先机械切块再逐块判定
    （blockpipe.py，块数由代码定死，不靠模型分块）。
  - provider：设置页里启用的那套 LLM 配置（providers.ProviderConfig）。
    传 None 时回落 project-alpha 的 DeepSeekClient + 其 .env 里的 key。

图片落地方式与 quizbank-web 不同：这里没有 web 路由伺服图片，而是把转换产出
的图直接拷进 `config.ASSETS_DIR`（题库 vault 的 `_assets/`），md 里的引用改写
成 Obsidian 双链嵌入 `![[<scope>_<file>]]`——转换阶段题目还没有 id（要等校对页
确认入库才分配），所以用来源文件名（scope）+原文件名做去重前缀，而不是等 id
分配后再改名，这样中间产物（预览用的 md 文本）从一开始就是最终形态，不需要
「先临时引用、入库后再重写一遍」这一步。
"""

import os
import re
import sys
import threading
import logging
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

import config
import llm_client

logger = logging.getLogger(__name__)

# project-alpha 的中间产物根目录（MinerU 解压的 md 与图片都落在这里）。
_RAW_MD_ROOT = Path(config.PROJECT_ALPHA) / "output" / "raw_md"


def _raw_md_dir(stem: str) -> Path:
    """某个输入文件对应的中间产物目录（绝对路径）。"""
    return _RAW_MD_ROOT / stem


# 切 CWD 用的互斥锁：只有读 project-alpha 配置这一步还需要 CWD 在其根下
# （dotenv.load_dotenv 在挂调试器等场景会退到按 CWD 找 .env）。
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

# MinerU 对图片直传有单独的硬限制（实测报 file size exceeds limit(10MB)）。
# 留安全余量，图片超过此阈值就先在本地转成单页 PDF 再走 PDF 上传通道。
_IMAGE_DIRECT_LIMIT_BYTES = 8 * 1024 * 1024


class ConvertError(Exception):
    """转换失败。"""


def _ensure_src_on_path():
    """把 project-alpha 根加入 sys.path，使其 `src` 包可 import。"""
    root = config.PROJECT_ALPHA
    if root not in sys.path:
        sys.path.insert(0, root)


def is_image_file(filename: str) -> bool:
    return Path(filename).suffix.lower() in _IMAGE_EXTS


def _oversized_image_to_pdf(file_path: Path) -> Path | None:
    """图片超过 MinerU 直传限制时，转成同目录同名单页 PDF 并返回其路径；
    未超限或转换失败则返回 None（调用方原样直传）。
    """
    try:
        if file_path.stat().st_size <= _IMAGE_DIRECT_LIMIT_BYTES:
            return None
    except OSError:
        return None
    out_path = file_path.with_suffix(".pdf")
    try:
        images_to_pdf([file_path], out_path)
    except ConvertError as e:
        logger.warning("[WARN] 大图转 PDF 失败，仍尝试直传: %s: %s", file_path, e)
        return None
    logger.info("[OK] 图片超过直传限制(%d MB)，已转为 PDF: %s",
               _IMAGE_DIRECT_LIMIT_BYTES // (1024 * 1024), out_path)
    return out_path


def images_to_pdf(image_paths, out_path) -> Path:
    """把多张图片按给定顺序合成一个 PDF（每张一页），返回 out_path。"""
    from PIL import Image, ImageOps

    imgs = []
    for p in image_paths:
        try:
            im = Image.open(p)
            im = ImageOps.exif_transpose(im)   # 摆正手机横拍/竖拍
            if im.mode != "RGB":
                im = im.convert("RGB")
            imgs.append(im)
        except Exception as e:
            logger.warning("[WARN] 图片无法读取，已跳过 %s: %s", p, e)
    if not imgs:
        raise ConvertError("没有可合成的有效图片")
    out_path = Path(out_path)
    imgs[0].save(str(out_path), "PDF", save_all=True, append_images=imgs[1:])
    logger.info("[OK] 合成 %d 张图片 -> %s", len(imgs), out_path)
    return out_path


def _make_llm_client(cfg, provider):
    """规范化用的 LLM 客户端。

    provider 有值 → 用本项目自己的 LLMClient；provider 为 None → 回落
    project-alpha 的 DeepSeekClient（老行为）。两者都满足 normalize() 需要的
    鸭子接口 chat(system, user) -> (content, finish_reason)。
    """
    if provider is not None:
        logger.info("规范化使用 LLM 配置: %s model=%s max_tokens=%s",
                    provider.label, provider.model, provider.max_tokens)
        return llm_client.build_client(provider)

    from src.deepseek_client import DeepSeekClient
    logger.info("规范化使用内置默认 DeepSeek: model=%s", cfg.deepseek_model)
    return DeepSeekClient(cfg.deepseek_api_key, cfg.deepseek_model)


# 识别引擎：
#   "whole" —— 老路径，整篇交给 project-alpha 的 normalize，块数由模型决定；
#   "block" —— 新路径，先机械切块再逐块判定（blockpipe），块数由代码定死。
ENGINE_WHOLE = "whole"
ENGINE_BLOCK = "block"


def _run_block_engine(raw_md: str, cfg, provider, *, include_solution: bool,
                      keep_images: bool, only_numbers, artifact_dir,
                      name: str) -> str:
    import blockpipe

    client = _make_llm_client(cfg, provider)
    return blockpipe.run(raw_md, client, keep_images=keep_images,
                         include_solution=include_solution,
                         only_numbers=only_numbers,
                         artifact_dir=artifact_dir, name=name)


def convert_file(file_path, *, is_image=False, include_solution=False,
                 keep_images=True, only_numbers=None, provider=None,
                 engine: str = ENGINE_WHOLE) -> str:
    """把一个 PDF/图片文件转换为规范化 md 文本。

    file_path: 待转换文件的绝对路径。
    is_image: 图片走 MinerU 直传（预留点①）。
    include_solution: 是否同时规范化解析（预留点②，透传给 run_parse）。
    keep_images: 是否保留题目插图（默认 True）。保留时把 MinerU 解析出的图
        拷到 config.ASSETS_DIR，md 里路径改写为 Obsidian 嵌入 `![[<scope>_<file>]]`。
    only_numbers: 仅导入指定题号的题（如 [8,11,14,18,19] 压轴题）。None=全部。
    provider: 规范化用的 LLM 配置（providers.ProviderConfig）。None=用
        project-alpha .env 里的集中 DeepSeek（老行为）。
    engine: ENGINE_WHOLE（默认，老的整篇规范化）或 ENGINE_BLOCK（先机械切块
        再逐块判定）。输出格式两者相同，下游无差别。
    返回规范化 md 文本。失败抛 ConvertError。
    """
    file_path = Path(file_path).resolve()
    if not file_path.is_file():
        raise ConvertError(f"文件不存在: {file_path}")
    is_image_input = is_image or is_image_file(file_path.name)
    if is_image_input:
        pdf_path = _oversized_image_to_pdf(file_path)
        if pdf_path is not None:
            file_path = pdf_path
            is_image_input = False

    _ensure_src_on_path()
    try:
        from src.config import load_config
        with _alpha_cwd():
            cfg = load_config()

        if is_image_input:
            return _convert_image(file_path, cfg, keep_images, only_numbers,
                                  provider, engine)
        return _convert_pdf(file_path, cfg, include_solution, keep_images,
                            only_numbers, provider, engine)
    except ConvertError:
        raise
    except llm_client.LLMClientError as e:
        raise ConvertError(str(e)) from e
    except Exception as e:
        raise ConvertError(f"转换失败: {type(e).__name__}: {e}") from e


def _convert_pdf(file_path: Path, cfg, include_solution: bool,
                 keep_images: bool = True, only_numbers=None,
                 provider=None, engine: str = ENGINE_WHOLE) -> str:
    """PDF/Word → 规范化 md。两种编排：

    - 老配置（engine=whole 且没配识别模型）：仍走 project-alpha 的 run_parse。
    - 其余情况：在这里自编排 MinerU + LLM（run_parse 内部自己 new
      DeepSeekClient，外面换不掉 LLM，逐块路径与「更换模型」都必须绕开它）。
    """
    if engine != ENGINE_BLOCK and provider is None:
        from src.pipeline import run_parse
        out_path = run_parse(
            pdf_path=str(file_path),
            config=cfg,
            raw_output_dir=_RAW_MD_ROOT,
            include_solution=include_solution,
            strict=False,
            keep_images=keep_images,
            only_numbers=only_numbers,
        )
        if out_path is None:
            raise ConvertError("该文件被识别为答案/解析文件，已跳过")
        md = Path(out_path).read_text(encoding="utf-8")
        if keep_images:
            md = _intercept_images(md, Path(out_path).parent, file_path.stem)
        return md

    from src.mineru_client import MineruClient
    from src.normalizer import normalize
    from src.pipeline import _cleanup_temp

    file_path = _prep_for_mineru(file_path)
    mineru = MineruClient(cfg.mineru_token, cfg.mineru_model_version)
    extract_dir = _raw_md_dir(file_path.stem)
    raw_md, _ = mineru.parse_pdf(file_path, extract_dir=extract_dir)
    (extract_dir / f"{file_path.stem}_raw.md").write_text(raw_md, encoding="utf-8")

    if engine == ENGINE_BLOCK:
        md = _run_block_engine(
            raw_md, cfg, provider, include_solution=include_solution,
            keep_images=keep_images, only_numbers=only_numbers,
            artifact_dir=extract_dir, name=file_path.stem)
    else:
        client = _make_llm_client(cfg, provider)
        md = normalize(raw_md, client, include_solution=include_solution,
                       keep_images=keep_images, only_numbers=only_numbers)
    (extract_dir / f"{file_path.stem}_normalized.md").write_text(
        md, encoding="utf-8")

    if keep_images:
        md = _intercept_images(md, extract_dir, file_path.stem)
    _cleanup_temp(extract_dir, file_path.stem, keep_images=keep_images)
    return md


def convert_exam_and_solution(exam_path, solution_path, only_numbers=None,
                              provider=None,
                              engine: str = ENGINE_WHOLE) -> str:
    """题干文件 + 单独的解析/答案文件 → 合并后一次规范化，按题号关联解析。"""
    exam_path = Path(exam_path).resolve()
    sol_path = Path(solution_path).resolve()
    if not exam_path.is_file():
        raise ConvertError(f"题干文件不存在: {exam_path}")
    if not sol_path.is_file():
        raise ConvertError(f"解析文件不存在: {sol_path}")

    _ensure_src_on_path()
    try:
        from src.config import load_config
        from src.mineru_client import MineruClient
        from src.normalizer import normalize

        with _alpha_cwd():
            cfg = load_config()
        mineru = MineruClient(cfg.mineru_token, cfg.mineru_model_version)

        exam_in = _prep_for_mineru(exam_path)
        sol_in = _prep_for_mineru(sol_path)

        def _parse(p: Path):
            return mineru.parse_pdf(p, extract_dir=_raw_md_dir(p.stem))

        with ThreadPoolExecutor(max_workers=2) as pool:
            fut_exam = pool.submit(_parse, exam_in)
            fut_sol = pool.submit(_parse, sol_in)
            exam_raw, _ = fut_exam.result()
            sol_raw, _ = fut_sol.result()

        combined = (exam_raw.rstrip()
                    + "\n\n# 参考答案与解析\n\n"
                    + sol_raw.lstrip())
        try:
            exam_dir = _raw_md_dir(exam_path.stem)
            exam_dir.mkdir(parents=True, exist_ok=True)
            (exam_dir / f"{exam_path.stem}_combined_raw.md").write_text(
                combined, encoding="utf-8")
        except OSError as e:
            logger.warning("[WARN] 合并原文落盘失败（不影响转换）: %s", e)

        if engine == ENGINE_BLOCK:
            md = _run_block_engine(
                combined, cfg, provider, include_solution=True,
                keep_images=True, only_numbers=only_numbers,
                artifact_dir=_raw_md_dir(exam_path.stem),
                name=exam_path.stem + "_combined")
        else:
            client = _make_llm_client(cfg, provider)
            md = normalize(combined, client, include_solution=True,
                           keep_images=True, only_numbers=only_numbers)
        md = _intercept_images(md, _raw_md_dir(exam_path.stem), exam_path.stem)
        md = _intercept_images(md, _raw_md_dir(sol_path.stem), sol_path.stem)
        return md
    except ConvertError:
        raise
    except llm_client.LLMClientError as e:
        raise ConvertError(str(e)) from e
    except Exception as e:
        raise ConvertError(f"转换失败: {type(e).__name__}: {e}") from e


def _prep_for_mineru(path: Path) -> Path:
    """把输入准备成 MinerU 能吃的文件：.docx/.doc 先转 PDF，大图片也转 PDF。"""
    if path.suffix.lower() in (".docx", ".doc"):
        from src.pipeline import _ensure_pdf
        return _ensure_pdf(path)
    if is_image_file(path.name):
        pdf_path = _oversized_image_to_pdf(path)
        if pdf_path is not None:
            return pdf_path
    return path


def _convert_image(file_path: Path, cfg, keep_images: bool = True,
                   only_numbers=None, provider=None,
                   engine: str = ENGINE_WHOLE) -> str:
    """图片（预留点①）：绕过 run_parse 的白名单，直接 MinerU + normalize。"""
    from src.mineru_client import MineruClient
    from src.normalizer import normalize

    mineru = MineruClient(cfg.mineru_token, cfg.mineru_model_version)
    extract_dir = _raw_md_dir(file_path.stem)
    raw_md, _ = mineru.parse_pdf(file_path, extract_dir=extract_dir)
    if engine == ENGINE_BLOCK:
        md = _run_block_engine(
            raw_md, cfg, provider, include_solution=False,
            keep_images=keep_images, only_numbers=only_numbers,
            artifact_dir=extract_dir, name=file_path.stem)
        if keep_images:
            md = _intercept_images(md, extract_dir, file_path.stem)
        return md
    client = _make_llm_client(cfg, provider)
    md = normalize(raw_md, client, include_solution=False,
                   keep_images=keep_images, only_numbers=only_numbers)
    if keep_images:
        md = _intercept_images(md, extract_dir, file_path.stem)
    return md


# 匹配 md 图片引用：![alt](images/xxx.jpg) —— 只认相对 images/ 路径（MinerU 输出格式）
_IMG_REF_RE = re.compile(r"!\[([^\]]*)\]\(\s*images/([^)\s]+)\s*\)")


def _intercept_images(md_text: str, extract_dir: Path, scope: str) -> str:
    """把 md 里 `![](images/xxx)` 的图从 extract_dir/images/ 拷到题库 vault 的
    `_assets/`，并把引用改写为 Obsidian 双链嵌入 `![[<scope>_<file>]]`。

    - 文件名前缀 scope（来源文件 stem）：避免不同来源的同名图（如都叫 img1.jpg）
      在扁平的 _assets/ 里互相覆盖；转换阶段题目还没有 id，不能按 id 命名。
    - 图缺失时保留原引用、记 warning，不中断转换。
    返回改写后的 md。extract_dir 相对/绝对均可（相对则以当前 CWD 解析）。
    """
    import shutil

    extract_dir = Path(extract_dir)
    dest_dir = config.ASSETS_DIR
    refs = list(_IMG_REF_RE.finditer(md_text))
    if not refs:
        return md_text

    dest_dir.mkdir(parents=True, exist_ok=True)
    copied = 0

    def _replace(m: "re.Match") -> str:
        nonlocal copied
        _alt, fname = m.group(1), m.group(2)
        # 防目录穿越：只取文件名部分
        safe_name = Path(fname).name
        src = extract_dir / "images" / safe_name
        dest_name = f"{scope}_{safe_name}"
        if not src.is_file():
            logger.warning("[WARN] 图片缺失，保留原引用: %s", src)
            return m.group(0)
        try:
            shutil.copy2(src, dest_dir / dest_name)
            copied += 1
        except OSError as e:
            logger.warning("[WARN] 拷贝图片失败 %s: %s", src, e)
            return m.group(0)
        return f"![[{dest_name}]]"

    new_md = _IMG_REF_RE.sub(_replace, md_text)
    logger.info("[OK] 拦截图片 %d 张 -> %s", copied, dest_dir)
    return new_md
