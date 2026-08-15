"""复用 Doc2X 整本缓存，机械拆分合集并原子地逐组写入文件式题库。

这个工具不重新调用 OCR，也不做人工补题；会用同一份布局 JSON 幂等修复合集缓存。
题干必须逐组形成从 1 开始的完整连续题号；解析按同组题号配对，缺少的解析允许留空，
多出的解析只记入报告而不生成题目。
写入使用稳定作用域，进程中断后重复执行不会制造第二份题。
"""

from __future__ import annotations

import argparse
from collections import Counter
import dataclasses
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        Path(tmp_name).unlink(missing_ok=True)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _duplicates(values) -> list[int]:
    counts = Counter(value for value in values if isinstance(value, int))
    return sorted(value for value, count in counts.items() if count > 1)


def _continuous_numbers(values) -> bool:
    numbers = [value for value in values if isinstance(value, int)]
    return (bool(numbers) and len(numbers) == len(set(numbers))
            and sorted(numbers) == list(range(1, max(numbers) + 1)))


def _validate_target(bank_dir: Path, parent_name: str, *, refresh: bool) -> None:
    target = bank_dir / parent_name
    if refresh:
        if not target.is_dir() or target.is_symlink():
            raise ValueError(f"待刷新的父文件夹不存在或不可用：{target}")
        return
    if not target.exists():
        return
    if not target.is_dir() or target.is_symlink():
        raise ValueError(f"目标不是可用文件夹：{target}")
    # 新建模式只接受空目标；已有稳定作用域必须显式走 --refresh-existing，不能在
    # 这里把含题目的目录当成空目录继续覆盖。
    if any(target.rglob("*")):
        raise ValueError(f"目标文件夹已有内容，拒绝覆盖：{target}")


def _load_runtime(args):
    os.environ["QUIZFORGE_BANK"] = str(args.bank.resolve())
    os.environ["QUIZFORGE_DATA_DIR"] = str(args.data_dir.resolve())
    os.environ["QUIZFORGE_DESKTOP"] = "1"
    os.environ["QUIZFORGE_SUBJECT"] = "physics"

    import app
    import blockpipe
    import blocksplit
    import config
    import converter
    import dedup
    import filestore

    if config.BANK_DIR.resolve() != args.bank.resolve():
        raise RuntimeError("题库环境变量未在模块导入前生效")
    return app, blockpipe, blocksplit, config, converter, dedup, filestore


def run(args) -> dict:
    (app, blockpipe, blocksplit, config, converter, dedup,
     filestore) = _load_runtime(args)

    for path in (args.exam_pdf, args.solution_pdf):
        if not path.is_file():
            raise FileNotFoundError(str(path))
    for path in (args.exam_cache, args.solution_cache):
        if not path.is_dir():
            raise FileNotFoundError(str(path))
    args.bank.mkdir(parents=True, exist_ok=True)
    parent_name = filestore.safe_folder_name(args.parent)
    if not parent_name:
        raise ValueError("父文件夹名称无效")
    _validate_target(args.bank.resolve(), parent_name,
                     refresh=args.refresh_existing)

    report = {
        "schema": 1,
        "mode": "doc2x+mechanical",
        "bank": str(args.bank.resolve()),
        "parent": parent_name,
        "exam_pdf": str(args.exam_pdf.resolve()),
        "solution_pdf": str(args.solution_pdf.resolve()),
        "exam_sha256": _sha256(args.exam_pdf),
        "solution_sha256": _sha256(args.solution_pdf),
        "units": [],
        "written": False,
    }
    units = []
    try:
        units = converter.recognize_collection_units(
            args.exam_pdf, args.solution_pdf,
            ocr_backend=converter.OCR_DOC2X,
            cache_dirs=[args.exam_cache, args.solution_cache])
        report["detected_units"] = len(units)
        if args.expected_units is not None and len(units) != args.expected_units:
            raise ValueError(
                f"识别出 {len(units)} 组，预期 {args.expected_units} 组")

        prepared = []
        total_questions = 0
        for index, unit in enumerate(units, 1):
            notes: list[str] = []
            pending = converter.convert_collection_unit_to_blocks(
                unit["raw_path"], source_name=unit["title"],
                source_pdf=args.exam_pdf, ocr_backend=converter.OCR_DOC2X,
                ocr_meta=unit.get("ocr_meta") or {}, note_sink=notes.append)
            blocks = [blocksplit.Block(**row) for row in pending["blocks"]]
            pairing = blocksplit.pair_blocks(blocks)
            rendered = blockpipe.render_without_ai(
                blocks, include_solution=True)
            preview, _folders, missing_numbers = app._build_import_preview(
                rendered, include_solution=True, existing_fps=set(), all_cols=[])
            numbers = [row.get("number") for row in preview]
            duplicate_numbers = _duplicates(numbers)
            body_counts = Counter(dedup.fingerprint(row["body"])
                                  for row in preview)
            duplicate_bodies = sum(
                count - 1 for count in body_counts.values() if count > 1)
            if missing_numbers or not _continuous_numbers(numbers):
                raise ValueError(
                    f"第 {index} 组「{unit['title']}」题干题号不完整：{numbers}")
            if duplicate_numbers or duplicate_bodies:
                raise ValueError(
                    f"第 {index} 组「{unit['title']}」存在重复："
                    f"题号 {duplicate_numbers}，正文 {duplicate_bodies} 道")

            missing_solutions = [
                row["number"] for row in preview if not row.get("solution", "").strip()
            ]
            orphan_solutions = [block.number for block in pairing.orphan_solutions]
            row = {
                "index": index,
                "title": unit["title"],
                "question_count": len(preview),
                "numbers": numbers,
                "missing_solution_numbers": missing_solutions,
                "orphan_solution_numbers": orphan_solutions,
                "pairing_conflicts": pairing.conflicts,
                "notes": notes,
            }
            report["units"].append(row)
            total_questions += len(preview)
            prepared.append((unit, pending, preview, rendered))
        report["question_count"] = total_questions
        report["solution_count"] = sum(
            row["question_count"] - len(row["missing_solution_numbers"])
            for row in report["units"])
        report["missing_solution_count"] = sum(
            len(row["missing_solution_numbers"]) for row in report["units"])
        report["orphan_solution_count"] = sum(
            len(row["orphan_solution_numbers"]) for row in report["units"])
        if (args.expected_questions is not None
                and total_questions != args.expected_questions):
            raise ValueError(
                f"机械拆出 {total_questions} 题，预期 {args.expected_questions} 题")
        _atomic_write_json(args.report, report)
        if not args.confirm_write:
            return report

        parent_id = filestore.get_or_create_collection(parent_name)
        imported = 0
        for unit, pending, preview_before, _rendered in prepared:
            # 到这里所有 21 组都已通过题干完整性门，才允许写图片与题目。
            final_md = converter.finish_block_review(
                pending, action="skip", include_solution=True)
            preview, _folders, missing_numbers = app._build_import_preview(
                final_md, include_solution=True, existing_fps=set(), all_cols=[])
            before_numbers = [row.get("number") for row in preview_before]
            after_numbers = [row.get("number") for row in preview]
            if missing_numbers or after_numbers != before_numbers:
                raise RuntimeError(
                    f"图片归档前后题号变化，拒绝写入：{unit['title']}")
            child_name = filestore.safe_folder_name(unit["title"])
            child_id = filestore.get_or_create_collection(child_name, parent_id)
            items = app._auto_import_items(preview, unit["title"])
            scope = (f"doc2x-collection:{report['exam_sha256']}:"
                     f"{report['solution_sha256']}:{unit['title']}")
            if args.refresh_existing:
                records = [
                    record for record in filestore.list_questions(
                        collection=child_id)
                    if (record.get("_meta") or {}).get(
                        "_quizforge_import_scope") == scope
                ]
                records.sort(key=lambda record: int(
                    (record.get("_meta") or {}).get(
                        "_quizforge_import_index", -1)))
                previous = [{
                    key: record.get(key)
                    for key in (
                        "body", "solution", "type", "source", "number",
                        "img_split", "img_layouts", "sol_img_split",
                        "sol_img_layouts",
                    )
                } for record in records]
                created = filestore.refresh_questions_batch(
                    items, previous, child_id, idempotency_scope=scope)
            else:
                created = filestore.create_questions_batch(
                    items, child_id, idempotency_scope=scope)
            imported += len(created)

        # 原卷只在父文件夹各保存一份；子文件夹只含题目。
        if args.refresh_existing:
            parent_path = args.bank / parent_name
            existing = [parent_path / args.exam_pdf.name,
                        parent_path / args.solution_pdf.name]
            expected = [report["exam_sha256"], report["solution_sha256"]]
            for path, digest in zip(existing, expected):
                if not path.is_file() or _sha256(path) != digest:
                    raise ValueError(f"父文件夹中的源 PDF 缺失或内容变化：{path.name}")
            exam_name, solution_name = (path.name for path in existing)
            report["refreshed_count"] = imported
        else:
            exam_name = filestore.store_paper(
                args.exam_pdf, parent_id, args.exam_pdf.name, "exam")
            solution_name = filestore.store_paper(
                args.solution_pdf, parent_id, args.solution_pdf.name, "solution")
            report["imported_count"] = imported
        report["source_files"] = [exam_name, solution_name]
        report["written"] = True
        _atomic_write_json(args.report, report)
        return report
    finally:
        for unit in units:
            workspace = unit.get("workspace_dir")
            if workspace:
                converter.cleanup_collection_workspace(workspace)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="复用 Doc2X 缓存，机械拆分合集并写入物理题库")
    parser.add_argument("--exam-pdf", type=Path, required=True)
    parser.add_argument("--solution-pdf", type=Path, required=True)
    parser.add_argument("--exam-cache", type=Path, required=True)
    parser.add_argument("--solution-cache", type=Path, required=True)
    parser.add_argument("--bank", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--parent", required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--expected-units", type=int)
    parser.add_argument("--expected-questions", type=int)
    parser.add_argument("--confirm-write", action="store_true")
    parser.add_argument(
        "--refresh-existing", action="store_true",
        help="安全刷新同一稳定作用域的既有导入；检测到用户编辑即拒绝覆盖")
    return parser.parse_args(argv)


def main() -> int:
    args = parse_args()
    report = run(args)
    print(json.dumps({
        "detected_units": report.get("detected_units"),
        "question_count": report.get("question_count"),
        "solution_count": report.get("solution_count"),
        "missing_solution_count": report.get("missing_solution_count"),
        "orphan_solution_count": report.get("orphan_solution_count"),
        "written": report.get("written"),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        raise SystemExit(1)
