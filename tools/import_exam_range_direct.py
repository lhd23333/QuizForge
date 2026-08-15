"""把连续年份的高考试卷直接转换并写入题库，支持中断续跑与失败隔离。

这是用户明确选择“全量直入、之后自行抽检”时使用的工具。题号断档和选项残缺会
记录到报告但不阻断；卷内重复、重复题号和写入失败仍隔离该卷，避免制造静默重复。
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import app
import config
import dedup
import mechfix
from tools import import_reviewed_batch, submit_exam_year


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=path.parent, prefix=f"{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        try:
            Path(tmp_name).unlink(missing_ok=True)
        except OSError:
            pass


def _ready_stems(target_year: Path) -> set[str]:
    if not target_year.is_dir():
        return set()
    return {
        folder.name for folder in target_year.iterdir()
        if (folder.is_dir() and any(folder.glob("*.md"))
            and any(folder.glob("*.pdf")))
    }


def _batch_snapshot(batch_id: str) -> dict:
    store = json.loads(config.TASKS_PATH.read_text(encoding="utf-8"))
    entry = store.get("batch", {}).get(batch_id)
    if not entry:
        raise ValueError(f"批次快照不存在：{batch_id}")
    return entry["payload"]


def _reusable_statuses(year: int, filenames: set[str]) -> list[dict]:
    """找出指定年份已转换完成、但目标文件夹尚未齐全的历史批次组。"""
    if not filenames or not config.TASKS_PATH.is_file():
        return []
    store = json.loads(config.TASKS_PATH.read_text(encoding="utf-8"))
    batches = store.get("batch", {})
    statuses = []
    for batch_id, entry in reversed(list(batches.items())):
        payload = entry.get("payload") if isinstance(entry, dict) else None
        if (not isinstance(payload, dict)
                or str(payload.get("pack_folder_name")) != str(year)):
            continue
        groups = []
        for group in payload.get("groups", []):
            if (group.get("filename") not in filenames
                    or group.get("status") != "done" or not group.get("md")):
                continue
            groups.append({
                "gid": int(group["gid"]),
                "filename": group["filename"],
                "status": "done",
                "error": None,
            })
        if groups:
            statuses.append({"batch_id": batch_id, "status": "done",
                             "groups": groups})
    return statuses


def _inspect_group(group: dict) -> dict:
    rows, _folders, _missing = app._build_import_preview(
        group["md"], existing_fps=set(), all_cols=[])
    numbers = sorted({row.get("number") for row in rows
                      if isinstance(row.get("number"), int)})
    missing = ([number for number in range(numbers[0], numbers[-1] + 1)
                if number not in numbers] if len(numbers) >= 2 else [])
    fingerprints = Counter(dedup.fingerprint(row["body"]) for row in rows)
    duplicate_bodies = sum(
        count - 1 for count in fingerprints.values() if count > 1)
    number_counts = Counter(row.get("number") for row in rows)
    duplicate_numbers = sorted(
        number for number, count in number_counts.items()
        if number is not None and count > 1)
    bad_choices = [
        row.get("number") for row in rows
        if (row["type"] in ("单选题", "多选题")
            and not mechfix.has_complete_choice_options(
                row["body"], known_choice=True))
    ]
    return {
        "question_count": len(rows),
        "missing_numbers": missing,
        "duplicate_body_count": duplicate_bodies,
        "duplicate_numbers": duplicate_numbers,
        "noncanonical_choice_numbers": bad_choices,
    }


def _target_has_questions(parent: str, year: int, filename: str) -> bool:
    folder = config.BANK_DIR / parent / str(year) / Path(filename).stem
    return folder.is_dir() and any(folder.glob("*.md"))


def _process_batch(base_url: str, parent: str, year: int,
                   status: dict) -> list[dict]:
    batch_id = status["batch_id"]
    batch = _batch_snapshot(batch_id)
    by_gid = {int(group["gid"]): group for group in batch["groups"]}
    records = []
    safe_gids = []
    record_by_gid = {}
    for summary in status.get("groups", []):
        gid = int(summary["gid"])
        record = {
            "batch_id": batch_id,
            "gid": gid,
            "filename": summary["filename"],
            "convert_status": summary.get("status"),
            "convert_error": summary.get("error"),
            "import_status": "not_attempted",
        }
        records.append(record)
        record_by_gid[gid] = record
        group = by_gid.get(gid)
        if not group or group.get("status") != "done" or not group.get("md"):
            continue
        inspection = _inspect_group(group)
        record.update(inspection)
        safe_gids.append(gid)

    if not safe_gids:
        return records
    try:
        imported = import_reviewed_batch.import_groups(
            base_url, batch_id, safe_gids, allow_missing=True,
            allow_duplicates=True)
        imported_by_gid = {int(row["gid"]): row for row in imported}
        for gid in safe_gids:
            row = imported_by_gid[gid]
            record_by_gid[gid].update(
                import_status="imported", imported=row["imported"])
        return records
    except Exception as exc:
        # 一组失败时先识别前面已成功写入的卷，再逐卷补剩余，保证失败不拖垮整批。
        batch_error = str(exc)
        for gid in safe_gids:
            record = record_by_gid[gid]
            if _target_has_questions(parent, year, record["filename"]):
                record["import_status"] = "imported_before_batch_error"
                continue
            try:
                row = import_reviewed_batch.import_groups(
                    base_url, batch_id, [gid], allow_missing=True,
                    allow_duplicates=True)[0]
                record.update(import_status="imported", imported=row["imported"])
            except Exception as group_exc:
                record.update(import_status="error",
                              import_error=str(group_exc),
                              batch_error=batch_error)
        return records


def run_range(source_root: Path, parent: str, start: int, end: int,
              base_url: str, report_path: Path) -> dict:
    report = {
        "schema": 1,
        "range": [start, end],
        "source_root": str(source_root.resolve()),
        "target_root": str((config.BANK_DIR / parent).resolve()),
        "started_at": time.time(),
        "years": [],
    }
    for year in range(end, start - 1, -1):
        source_year = source_root / str(year)
        pdfs = sorted(source_year.glob("*.pdf"))
        target_year = config.BANK_DIR / parent / str(year)
        ready = _ready_stems(target_year)
        pending = [path for path in pdfs if path.stem not in ready]
        year_row = {
            "year": year,
            "source_count": len(pdfs),
            "already_complete": len(ready),
            "submitted_count": len(pending),
            "records": [],
        }
        report["years"].append(year_row)
        _atomic_write_json(report_path, report)
        print(json.dumps({"event": "year_start", "year": year,
                          "source": len(pdfs), "pending": len(pending)},
                         ensure_ascii=False), flush=True)

        # 上次暂停可能发生在“整年已转换、只写入了一部分”之间。先复用持久化批次，
        # 再决定是否付费提交；否则 2009 这类断点会把剩余 28 卷重新 OCR 一遍。
        reusable = _reusable_statuses(year, {path.name for path in pending})
        year_row["reused_batch_count"] = len(reusable)
        for old_status in reusable:
            year_row["records"].extend(
                _process_batch(base_url, parent, year, old_status))
        ready = _ready_stems(target_year)
        pending = [path for path in pdfs if path.stem not in ready]
        year_row["submitted_count"] = len(pending)
        if not pending:
            year_row["complete_count"] = len(ready)
            year_row["remaining"] = []
            year_row["anomaly_count"] = sum(
                bool(record.get("missing_numbers")
                     or record.get("noncanonical_choice_numbers")
                     or record.get("duplicate_numbers")
                     or record.get("duplicate_body_count"))
                for record in year_row["records"])
            _atomic_write_json(report_path, report)
            print(json.dumps({"event": "year_done", "year": year,
                              "complete": len(ready), "remaining": 0,
                              "anomalies": year_row["anomaly_count"],
                              "reused_batches": len(reusable)},
                             ensure_ascii=False), flush=True)
            continue

        status = submit_exam_year.submit_year(
            base_url, source_year, parent, engine="block", block_mode="no_ai",
            only={path.name for path in pending})
        year_row["records"].extend(
            _process_batch(base_url, parent, year, status))

        failed_names = {
            record["filename"] for record in year_row["records"]
            if record["convert_status"] == "error"
        }
        if failed_names:
            print(json.dumps({"event": "retry_conversion", "year": year,
                              "count": len(failed_names)},
                             ensure_ascii=False), flush=True)
            retry = submit_exam_year.submit_year(
                base_url, source_year, parent, engine="block", block_mode="no_ai",
                only=failed_names)
            year_row["records"].extend(
                _process_batch(base_url, parent, year, retry))

        ready_after = _ready_stems(target_year)
        year_row["complete_count"] = len(ready_after)
        year_row["remaining"] = sorted(path.name for path in pdfs
                                       if path.stem not in ready_after)
        year_row["anomaly_count"] = sum(
            bool(record.get("missing_numbers")
                 or record.get("noncanonical_choice_numbers"))
            for record in year_row["records"])
        _atomic_write_json(report_path, report)
        print(json.dumps({"event": "year_done", "year": year,
                          "complete": len(ready_after),
                          "remaining": len(year_row["remaining"]),
                          "anomalies": year_row["anomaly_count"]},
                         ensure_ascii=False), flush=True)
    report["finished_at"] = time.time()
    report["summary"] = {
        "source_count": sum(row["source_count"] for row in report["years"]),
        "complete_count": sum(row.get("complete_count", row["already_complete"])
                              for row in report["years"]),
        "remaining_count": sum(len(row.get("remaining", []))
                               for row in report["years"]),
    }
    _atomic_write_json(report_path, report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="直接转换并导入连续年份高考试卷")
    parser.add_argument("source_root", type=Path)
    parser.add_argument("--parent", required=True)
    parser.add_argument("--start", type=int, required=True)
    parser.add_argument("--end", type=int, required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:5000")
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--confirm-paid", action="store_true")
    parser.add_argument("--confirm-write", action="store_true")
    args = parser.parse_args()
    if not args.confirm_paid or not args.confirm_write:
        parser.error("该操作会调用付费 OCR 并写入真实题库，必须同时确认")
    if args.start > args.end:
        parser.error("--start 不能大于 --end")
    report = run_range(
        args.source_root, args.parent.strip("/"), args.start, args.end,
        args.base_url.rstrip("/"), args.report)
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    return 1 if report["summary"]["remaining_count"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
