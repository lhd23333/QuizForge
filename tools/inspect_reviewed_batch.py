"""只读检查一个已转换批次的默认校对结果。"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import app
import config
import dedup
import mechfix


def inspect_batch(batch_id: str) -> dict:
    store = json.loads(config.TASKS_PATH.read_text(encoding="utf-8"))
    entry = store.get("batch", {}).get(batch_id)
    if not entry:
        raise ValueError("批次快照不存在")
    rows = []
    for group in entry["payload"]["groups"]:
        row = {"gid": group["gid"], "filename": group["filename"],
               "status": group["status"], "notes": group.get("notes") or []}
        if group.get("status") == "done" and group.get("md"):
            # 年份批次结构体检不需要跨卷查重，跳过全库扫描；卷内重复在下方单独算。
            preview, _folders, missing = app._build_import_preview(
                group["md"], existing_fps=set(), all_cols=[])
            types = Counter(item["type"] for item in preview)
            fingerprints = Counter(
                dedup.fingerprint(item["body"]) for item in preview)
            noncanonical = [
                item.get("number") for item in preview
                if item["type"] in ("单选题", "多选题")
                and set(app._CANON_OPTION_RE.findall(item["body"]))
                != {"A", "B", "C", "D"}
            ] if hasattr(app, "_CANON_OPTION_RE") else [
                item.get("number") for item in preview
                if item["type"] in ("单选题", "多选题")
                and not mechfix.has_complete_choice_options(item["body"])
            ]
            row.update({
                "question_count": len(preview),
                "numbers": [item["number"] for item in preview],
                "missing_numbers": missing or [],
                "types": dict(sorted(types.items())),
                "duplicate_count": sum(
                    count - 1 for count in fingerprints.values() if count > 1),
                "noncanonical_choice_numbers": noncanonical,
            })
        rows.append(row)
    return {"batch_id": batch_id, "groups": rows}


def main() -> int:
    parser = argparse.ArgumentParser(description="只读检查已转换批次")
    parser.add_argument("batch_id")
    args = parser.parse_args()
    print(json.dumps(inspect_batch(args.batch_id), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        raise SystemExit(1)
