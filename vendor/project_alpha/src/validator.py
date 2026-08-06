"""验证层：检查 DeepSeek 规范化输出的质量。

在 normalizer 之后运行，生成验证报告。
默认仅告警不阻断，调用方可通过 strict 参数决定是否中止。
"""

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# raw.md 题目识别模式：数字 + 全角/半角点号 + 全角方括号【
_RAW_QUESTION_RE = re.compile(r"^(?:##\s*)?\d+[．.]\s*【(?!答案)", re.MULTILINE)

# 规范化输出题目识别：行首 `- ` 无序列表项
_NORM_QUESTION_RE = re.compile(r"^- ", re.MULTILINE)


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass
class ValidationResult:
    """单项验证的结果。"""

    name: str
    passed: bool
    total: int
    failed: int
    details: list[str] = field(default_factory=list)


@dataclass
class ValidationReport:
    """完整的验证报告。"""

    pdf_stem: str
    passed: bool
    question_count_raw: int
    question_count_normalized: int
    count_result: ValidationResult
    format_result: ValidationResult
    timestamp: str = field(
        default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    )


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def validate(
    raw_md: str,
    normalized_md: str,
    pdf_stem: str,
) -> ValidationReport:
    """验证 normalized_md 的完整性，返回报告。"""
    count_result = _check_question_count(raw_md, normalized_md)
    blocks = _split_by_question(normalized_md)
    format_result = _check_format(blocks)

    passed = count_result.passed and format_result.passed

    return ValidationReport(
        pdf_stem=pdf_stem,
        passed=passed,
        question_count_raw=_count_questions_raw(raw_md),
        question_count_normalized=len(blocks),
        count_result=count_result,
        format_result=format_result,
    )


# ---------------------------------------------------------------------------
# 题目切分（新格式：无序列表 `- `）
# ---------------------------------------------------------------------------


def _split_by_question(text: str) -> list[str]:
    """按顶层 ``- `` 无序列表项切分，返回每个题块。"""
    if not text or not text.strip():
        return []
    blocks: list[str] = []
    current: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        # 顶层的 `- ` 开始新题目
        if line.startswith("- ") and not line.startswith("  "):
            if current:
                blocks.append("\n".join(current).strip())
            current = [line]
        else:
            current.append(line)
    if current:
        blocks.append("\n".join(current).strip())
    return [b for b in blocks if b]


# ---------------------------------------------------------------------------
# 题数对比
# ---------------------------------------------------------------------------


def _count_questions_raw(raw_md: str) -> int:
    """统计 MinerU raw.md 中的题目数量。"""
    if not raw_md or not raw_md.strip():
        return 0
    return len(_RAW_QUESTION_RE.findall(raw_md))


def _check_question_count(raw_md: str, normalized_md: str) -> ValidationResult:
    """对比 raw.md 与 normalized.md 的题数。

    阈值：绝对差异 ≤ 2 或相对差异 ≤ 20% 视为通过。
    """
    raw_count = _count_questions_raw(raw_md)
    norm_count = len(_split_by_question(normalized_md))

    # 试卷模式：raw 无 `【】` 格式，无法对比，直接通过
    if raw_count == 0:
        return ValidationResult(
            name="题数对比",
            passed=True,
            total=2,
            failed=0,
            details=[f"raw.md 无【】标记（试卷模式），normalized.md {norm_count} 题"],
        )

    diff = abs(raw_count - norm_count)
    ratio = diff / max(raw_count, 1)
    passed = diff <= 2 or ratio <= 0.2
    details: list[str] = []

    if not passed:
        details.append(
            f"raw.md {raw_count} 题 vs normalized.md {norm_count} 题"
            f"（差异 {diff} 题，比例 {ratio:.0%}）"
        )
    else:
        details.append(
            f"raw.md {raw_count} 题 → normalized.md {norm_count} 题，差异在阈值内"
        )

    return ValidationResult(
        name="题数对比",
        passed=passed,
        total=2,
        failed=0 if passed else 1,
        details=details,
    )


# ---------------------------------------------------------------------------
# 格式检查
# ---------------------------------------------------------------------------


def _check_format(blocks: list[str]) -> ValidationResult:
    """检查每题是否以 ``- `` 无序列表项开头。"""
    total = len(blocks)
    failed = 0
    details: list[str] = []

    for i, block in enumerate(blocks, 1):
        first_line = block.splitlines()[0].strip() if block else ""
        if not first_line.startswith("- "):
            failed += 1
            preview = first_line[:60]
            details.append(f"第{i}题：首行不是 `- ` 开头（首行: {preview}）")
        elif len(first_line) <= 2:
            failed += 1
            details.append(f"第{i}题：`- ` 后内容为空")

    return ValidationResult(
        name="格式检查",
        passed=failed == 0,
        total=total,
        failed=failed,
        details=details,
    )


# ---------------------------------------------------------------------------
# 报告输出
# ---------------------------------------------------------------------------


def save_report(report: ValidationReport, output_dir: str | Path) -> Path:
    """将验证报告保存为 markdown 文件。"""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "validation_report.md"

    lines = _render_report(report)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    logger.info("验证报告已保存: %s", path)
    return path


def _render_report(report: ValidationReport) -> list[str]:
    """生成 markdown 报告文本。"""
    rows = [
        ("题数对比", report.count_result),
        ("格式检查", report.format_result),
    ]

    lines = [
        "# 验证报告",
        "",
        f"**文件**: {report.pdf_stem}",
        f"**时间**: {report.timestamp}",
        f"**总体**: {'✅ 通过' if report.passed else '⚠️ 存在问题'}",
        "",
        "## 概览",
        "",
        "| 检查项 | 结果 | 详情 |",
        "|--------|------|------|",
    ]

    for name, result in rows:
        status = "✅ PASS" if result.passed else f"❌ {result.failed} 项失败"
        detail = (
            f"{result.total} 项检查，{result.failed} 项失败"
            if result.failed > 0
            else "全部通过"
        )
        lines.append(f"| {name} | {status} | {detail} |")

    lines += [
        "",
        f"**题数**: raw.md {report.question_count_raw} 题 → "
        f"normalized.md {report.question_count_normalized} 题",
        "",
    ]

    for name, result in rows:
        lines.append(f"## {name}")
        lines.append("")
        if result.passed:
            lines.append(f"通过（{result.total} 项全部正常）")
        else:
            lines.append(f"失败 {result.failed}/{result.total} 项：")
            lines.append("")
            for d in result.details:
                lines.append(f"- {d}")
        lines.append("")

    return lines
