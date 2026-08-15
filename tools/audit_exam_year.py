"""核对一个年份的源试卷与 QuizForge 入库结果。

只读扫描，不修改题库。默认把 JSON 报告打印到 stdout；传 ``--output`` 时原子写入
指定文件，供逐年导入留档。人工仍需逐题对照 PDF，本工具负责先钉住可机械判断的
遗漏、重复、题号、附件、图片引用与明显格式问题。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import mechfix


_NUMBER_RE = re.compile(r"^第\s*(\d+)\s*题$|^(\d+)$")
_WIKI_IMAGE_RE = re.compile(r"!\[\[([^\]|]+)(?:\|[^\]]*)?\]\]")
# 识别链常把选项字母一并包进 ``$\displaystyle A.$``，不能只认行首纯文本；规则与
# importer 的选项识别保持同一形状，但捕获字母供 A-D 完整性判断。
_OPTION_RE = re.compile(
    r"(?:\$\\displaystyle\s*)?(?:\(([A-D])\)|([A-D])(?:[.．]|\$(?=\s)))")
_CANON_OPTION_RE = re.compile(r"\$\\displaystyle\s*([A-D])[.．]\s*\$")
_MARKER_RE = re.compile(r"@@(?:KIND|TYPE|BODY|SOL)\b")
_SUB_TAG_RE = re.compile(r"</?sub(?:\s[^>]*)?>", re.I)
_SUP_TAG_RE = re.compile(r"</?sup(?:\s[^>]*)?>", re.I)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _frontmatter_and_body(path: Path) -> tuple[dict, str]:
    text = path.read_text(encoding="utf-8", errors="replace")
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}, text
    meta = {}
    for line in text[4:end].splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        meta[key.strip()] = value.strip().strip('"\'')
    return meta, text[end + 5:]


def _question_number(path: Path, meta: dict) -> int | None:
    raw = str(meta.get("number") or "").strip()
    if raw.isdigit():
        return int(raw)
    match = _NUMBER_RE.match(path.stem)
    if not match:
        return None
    return int(match.group(1) or match.group(2))


def _math_delimiters_balanced(text: str) -> bool:
    cleaned = re.sub(r"\\\$", "", text)
    cleaned = re.sub(r"\$\$.*?\$\$", "", cleaned, flags=re.S)
    return cleaned.count("$") % 2 == 0


def _audit_paper(folder: Path, assets_dir: Path, source_path: Path) -> dict:
    expected_source = source_path.stem
    md_files = sorted(folder.glob("*.md"))
    attachments = sorted(p for p in folder.iterdir()
                         if p.is_file() and p.suffix.lower() != ".md")
    numbers = []
    ids = []
    issues = []
    question_rows = []
    for path in md_files:
        meta, body = _frontmatter_and_body(path)
        number = _question_number(path, meta)
        if number is not None:
            numbers.append(number)
        qid = str(meta.get("id") or "")
        if qid:
            ids.append(qid)
        qissues = []
        if not body.strip():
            qissues.append("空题干")
        if str(meta.get("source") or "") != expected_source:
            qissues.append("题源与试卷文件夹名不一致")
        qtype = str(meta.get("type") or "")
        looks_choice = mechfix.looks_like_choice_options(body)
        if looks_choice and qtype not in ("单选题", "多选题"):
            qissues.append("含完整 A-D 选项但题型不是选择题")
        if qtype in ("单选题", "多选题"):
            options = {left or right for left, right in _OPTION_RE.findall(body)}
            if options != {"A", "B", "C", "D"}:
                qissues.append("选择题选项不完整")
            elif set(_CANON_OPTION_RE.findall(body)) != {"A", "B", "C", "D"}:
                qissues.append("选项标签未统一为 A. 格式")
        if _MARKER_RE.search(body):
            qissues.append("残留识别协议标记")
        if _SUB_TAG_RE.search(body):
            qissues.append("残留 MinerU <sub> 标签")
        if _SUP_TAG_RE.search(body):
            qissues.append("残留 MinerU <sup> 标签")
        if mechfix.normalize_misplaced_constraints(body) != body:
            qissues.append("最值题约束条件疑似被抽到答案位置")
        if not _math_delimiters_balanced(body):
            qissues.append("数学公式定界符疑似不配对")
        missing_images = [name for name in _WIKI_IMAGE_RE.findall(body)
                          if not (assets_dir / name).is_file()]
        if missing_images:
            qissues.append("引用图片不存在：" + "、".join(missing_images))
        if qissues:
            issues.extend(f"{path.name}：{item}" for item in qissues)
        question_rows.append({"file": path.name, "number": number,
                              "id": qid, "issues": qissues})

    duplicates = sorted(n for n, count in Counter(numbers).items() if count > 1)
    if duplicates:
        issues.append("重复题号：" + "、".join(map(str, duplicates)))
    missing = []
    if numbers:
        missing = sorted(set(range(min(numbers), max(numbers) + 1)) - set(numbers))
        if missing:
            issues.append("题号断档：" + "、".join(map(str, missing)))
    duplicate_ids = sorted(i for i, count in Counter(ids).items() if count > 1)
    if duplicate_ids:
        issues.append("重复题目 ID：" + "、".join(duplicate_ids))
    pdfs = [p for p in attachments if p.suffix.lower() == ".pdf"]
    if len(pdfs) != 1:
        issues.append(f"原 PDF 数量应为 1，实际 {len(pdfs)}")
    elif _sha256(pdfs[0]) != _sha256(source_path):
        issues.append("已保存原 PDF 与源文件内容不一致")
    return {
        "paper": folder.name,
        "question_count": len(md_files),
        "numbers": sorted(numbers),
        "missing_numbers": missing,
        "attachments": [p.name for p in attachments],
        "questions": question_rows,
        "issues": issues,
    }


def audit_year(source_year: Path, bank_year: Path, assets_dir: Path) -> dict:
    sources = sorted(source_year.glob("*.pdf"))
    source_rows = [{"file": p.name, "stem": p.stem, "bytes": p.stat().st_size,
                    "sha256": _sha256(p)} for p in sources]
    folders = {p.name: p for p in bank_year.iterdir() if p.is_dir()} \
        if bank_year.is_dir() else {}
    source_stems = {p.stem for p in sources}
    missing_folders = sorted(source_stems - set(folders))
    extra_folders = sorted(set(folders) - source_stems)
    source_by_stem = {p.stem: p for p in sources}
    papers = [_audit_paper(folders[stem], assets_dir, source_by_stem[stem])
              for stem in sorted(source_stems & set(folders))]
    return {
        "schema": 1,
        "year": source_year.name,
        "source_dir": str(source_year.resolve()),
        "bank_dir": str(bank_year.resolve()),
        "source_count": len(source_rows),
        "paper_folder_count": len(folders),
        "missing_paper_folders": missing_folders,
        "extra_paper_folders": extra_folders,
        "sources": source_rows,
        "papers": papers,
        "summary": {
            "question_count": sum(p["question_count"] for p in papers),
            "paper_issue_count": sum(bool(p["issues"]) for p in papers),
            "issue_count": sum(len(p["issues"]) for p in papers)
                           + len(missing_folders) + len(extra_folders),
        },
    }


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def main() -> int:
    parser = argparse.ArgumentParser(description="审计一个年份的高考试卷入库结果")
    parser.add_argument("source_year", type=Path)
    parser.add_argument("bank_year", type=Path)
    parser.add_argument("--assets", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = audit_year(args.source_year, args.bank_year, args.assets)
    text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        _write_atomic(args.output, text)
    else:
        print(text, end="")
    return 1 if report["summary"]["issue_count"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
