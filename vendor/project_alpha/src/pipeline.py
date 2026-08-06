"""parse 流程编排：Word/PDF -> MinerU 解析 -> DeepSeek 规范化 -> 验证 -> 规范化 Markdown。"""

import logging
import re
import subprocess
from pathlib import Path

from .config import Config
from .deepseek_client import DeepSeekClient
from .exceptions import PDFNormalizerError, ValidationError
from .mineru_client import MineruClient
from .normalizer import normalize
from .validator import save_report, validate

logger = logging.getLogger(__name__)

# 答案/解析文件识别——这些不是试卷，应跳过
_ANSWER_PATTERNS = re.compile(r"(答案|解析|解答|全解析|标答|参考解答)")

# 试卷模式：文件名清洗
_EXAM_DATE_RE = re.compile(r"(\d{4})年(\d{1,2})月")
_EXAM_SUFFIX_RE = re.compile(r"[-_]?(试卷|试题|试题卷|原卷|全解析|解析|答案|解答|标答|参考解答)$")
_EXAM_PREFIX_RE = re.compile(r"^【[^】]*?】")
_EXAM_SHORT_YEAR_RE = re.compile(r"(?<!\d)(\d{2})(?=年|数学|届|级)")


def _docx_to_pdf(docx_path: Path) -> Path:
    """用 Word COM 将 .docx 转为 .pdf，返回 pdf 路径。"""
    pdf_path = docx_path.with_suffix(".pdf")
    if pdf_path.exists():
        return pdf_path

    logger.info("Word 转 PDF: %s", docx_path.name)
    ps = (
        f"$w=New-Object -ComObject Word.Application;"
        f"$w.Visible=$false;"
        f"$d=$w.Documents.Open('{docx_path.resolve()}');"
        f"$d.SaveAs('{pdf_path.resolve()}', 17);"
        f"$d.Close();"
        f"$w.Quit()"
    )
    result = subprocess.run(
        ["powershell", "-Command", ps],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0:
        stderr = result.stderr.strip()
        raise PDFNormalizerError(f"Word 转 PDF 失败: {stderr}")
    if not pdf_path.exists():
        raise PDFNormalizerError(f"Word 转 PDF 未生成文件: {pdf_path}")
    logger.info("Word 转 PDF 完成: %s", pdf_path.name)
    return pdf_path


def _ensure_pdf(file_path: Path) -> Path:
    """确保输入是 PDF：.docx 自动转换，PDF 直接返回。"""
    suffix = file_path.suffix.lower()
    if suffix == ".pdf":
        return file_path
    if suffix in (".docx", ".doc"):
        return _docx_to_pdf(file_path)
    raise PDFNormalizerError(f"不支持的文件格式: {suffix}")


def _exam_source(filename_stem: str) -> str:
    """从试卷 PDF 文件名提取 source。"""
    s = filename_stem
    # 去【答案】【试卷】【2026.06.30】等前缀
    s = _EXAM_PREFIX_RE.sub("", s)
    # 去尾部标记（-原卷 -标答 答案 等）
    s = _EXAM_SUFFIX_RE.sub("", s)
    # 2位年份 → 4位（"26数学" → "2026数学"）
    s = _EXAM_SHORT_YEAR_RE.sub(r"20\1", s)
    # 日期规范化：2026年6月 → 2026.6
    s = _EXAM_DATE_RE.sub(r"\1.\2", s)
    # 再次去尾（处理不带分隔符的）
    s = re.sub(r"(试卷|试题|试题卷)$", "", s)
    return s.strip("-_")


def _detect_mode(pdf_path: Path) -> tuple[str, str, str | None, str]:
    """根据输入路径判断模式，返回 (mode, author, appear_or_source, sub_path)。

    sub_path 是 input/{模式}/ 到 PDF 父目录的相对路径，用于镜像到 题目/ 下。
    例: input/试卷/模考/2026/2026.6/xxx.pdf → sub_path="模考/2026/2026.6"
    """
    parts = pdf_path.resolve().parts
    try:
        input_idx = parts.index("input")
    except ValueError:
        return ("题集", "", pdf_path.stem, "")

    mode_dir_idx = input_idx + 1
    if mode_dir_idx >= len(parts):
        return ("题集", "", pdf_path.stem, "")

    mode = "题集" if parts[mode_dir_idx] == "题集" else "试卷" if parts[mode_dir_idx] == "试卷" else "题集"

    # author = 模式目录下的第一级子文件夹
    author = parts[mode_dir_idx + 1] if mode_dir_idx + 1 < len(parts) else ""

    # sub_path = author/... 一直到 PDF 的父目录
    pdf_parent_idx = len(parts) - 1  # PDF 文件名所在位置
    if pdf_parent_idx > mode_dir_idx + 1:
        sub_path = str(Path(*parts[mode_dir_idx + 1:pdf_parent_idx]))
    else:
        sub_path = author

    if mode == "试卷":
        source = _exam_source(pdf_path.stem)
        return ("试卷", author, source, sub_path)
    else:
        appear = pdf_path.stem
        return ("题集", author, appear, sub_path)


def run_parse(
    pdf_path: str | Path,
    config: Config,
    raw_output_dir: str | Path = "output/raw_md",
    include_solution: bool = False,
    strict: bool = False,
    keep_images: bool = False,
    only_numbers: list[int] | None = None,
) -> Path | None:
    """端到端 parse，返回规范化后的 .md 文件路径；跳过答案文件时返回 None。

    keep_images=True 时：normalize 保留图片引用，且清理阶段保留 images/ 目录
    （供 QuizForge 拦截拷贝）。默认 False 保持 project-alpha 原行为。
    only_numbers（默认 None=全部）：仅规范化指定题号的题，透传给 normalize。
    """
    pdf_path = Path(pdf_path)
    pdf_path = _ensure_pdf(pdf_path)  # .docx 自动转 PDF
    raw_output_dir = Path(raw_output_dir)
    raw_output_dir.mkdir(parents=True, exist_ok=True)

    # 0. 识别模式
    mode, author, appear_or_source, sub_path = _detect_mode(pdf_path)

    # 0.5 跳过答案/解析文件（试卷模式下）
    if mode == "试卷" and _ANSWER_PATTERNS.search(pdf_path.stem):
        logger.info("跳过答案/解析文件: %s", pdf_path.name)
        return None
    logger.info("模式=%s author=%s", mode, author)

    # 1. MinerU 解析 PDF
    mineru = MineruClient(config.mineru_token, config.mineru_model_version)
    extract_dir = raw_output_dir / pdf_path.stem
    raw_md, _md_name = mineru.parse_pdf(pdf_path, extract_dir=extract_dir)
    (extract_dir / f"{pdf_path.stem}_raw.md").write_text(raw_md, encoding="utf-8")

    # 2. DeepSeek 规范化
    deepseek = DeepSeekClient(config.deepseek_api_key, config.deepseek_model)
    logger.info("调用 DeepSeek 规范化...")
    # 试卷模式：把 source 注入草稿，让 DeepSeek 知道来源
    if mode == "试卷":
        raw_md = f"# 本试卷来源：{appear_or_source}\n\n" + raw_md
    normalized = normalize(raw_md, deepseek, include_solution=include_solution,
                           keep_images=keep_images, only_numbers=only_numbers)
    (extract_dir / f"{pdf_path.stem}_normalized.md").write_text(
        normalized, encoding="utf-8"
    )

    # 2.5 验证
    logger.info("验证规范化输出...")
    report = validate(raw_md, normalized, pdf_path.stem)
    report_path = save_report(report, extract_dir)

    if not report.count_result.passed:
        for d in report.count_result.details:
            logger.warning("验证 [题数对比]: %s", d)
    if not report.format_result.passed:
        for d in report.format_result.details:
            logger.warning("验证 [格式]: %s", d)

    if strict and not report.passed:
        raise ValidationError(f"严格模式：验证未通过，详见 {report_path}")

    # 3. 清理临时文件，仅保留规范化结果和验证报告（keep_images 时另保留 images/）
    _cleanup_temp(extract_dir, pdf_path.stem, keep_images=keep_images)

    # 4. 返回规范化文件路径
    normalized_path = extract_dir / f"{pdf_path.stem}_normalized.md"
    logger.info("parse 完成，规范化输出: %s", normalized_path)
    return normalized_path


def _cleanup_temp(extract_dir: Path, stem: str, keep_images: bool = False) -> None:
    """删除 MinerU 中间产物，仅保留 _normalized.md 和 validation_report.md。

    keep_images=True 时额外保留 images/ 目录（MinerU 解压出的题目插图），
    供 QuizForge 的 converter 拦截拷贝到持久目录。
    """
    keep = {f"{stem}_normalized.md", "validation_report.md"}
    keep_dirs = {"images"} if keep_images else set()
    for item in extract_dir.iterdir():
        if item.name in keep:
            continue
        if item.is_dir() and item.name in keep_dirs:
            continue
        if item.is_dir():
            import shutil
            shutil.rmtree(item, ignore_errors=True)
        else:
            item.unlink(missing_ok=True)
    logger.debug("已清理临时文件: %s（保留图片=%s）", extract_dir, keep_images)
