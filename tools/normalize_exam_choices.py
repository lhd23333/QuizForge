"""规范化指定题库子树里的选择题标签和解答题小问排版。

选择题只改能可靠识别出完整 A—D 四元组的题目；解答题只拆连续的（1）（2）……。
必须先在外部完成目标子树备份，并显式传 ``--confirm-write``。解析与元数据不动。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import filestore
import mechfix


def normalize_collection(collection: str, write: bool = False,
                         overrides: list[dict] | None = None) -> dict:
    rows = filestore.list_questions(collection=collection)
    changed = []
    subquestion_changed = []
    skipped = []
    for row in rows:
        if row["type"] == "解答题":
            fixed = mechfix.normalize_subquestion_layout(row["body"])
            if fixed != row["body"]:
                changed.append(row["path"])
                subquestion_changed.append(row["path"])
                if write:
                    filestore.update_question(
                        row["id"], fixed, row["solution"], row["type"],
                        row["source"], row["difficulty"], row["tags"])
            continue
        known_choice = row["type"] in ("单选题", "多选题")
        if not (known_choice or mechfix.looks_like_choice_options(row["body"])):
            continue
        fixed = mechfix.normalize_choice_options(
            row["body"], known_choice=known_choice)
        fixed_type = row["type"] if row["type"] == "多选题" else "单选题"
        if fixed == row["body"] and fixed_type == row["type"]:
            skipped.append(row["path"])
            continue
        changed.append(row["path"])
        if write:
            filestore.update_question(
                row["id"], fixed, row["solution"], fixed_type, row["source"],
                row["difficulty"], row["tags"])
    review_rows = [
        {"id": row["id"], "path": row["path"],
         "body_sha256": hashlib.sha256(row["body"].encode("utf-8")).hexdigest()}
        for row in rows
        if (row["type"] in ("单选题", "多选题")
            or mechfix.looks_like_choice_options(row["body"]))
        and not mechfix.has_complete_choice_options(
            row["body"], known_choice=row["type"] in ("单选题", "多选题"))
    ]
    applied_overrides = []
    by_id = {row["id"]: row for row in rows}
    for override in overrides or []:
        row = by_id.get(str(override.get("id") or ""))
        if not row:
            raise ValueError(f"修订目标不存在：{override.get('id')}")
        actual = hashlib.sha256(row["body"].encode("utf-8")).hexdigest()
        if actual != override.get("expected_body_sha256"):
            raise ValueError(f"{row['path']} 已被修改，拒绝覆盖人工校订")
        fixed_body = str(override.get("body") or "").strip()
        fixed_type = str(override.get("type") or row["type"])
        if not fixed_body:
            raise ValueError(f"{row['path']} 的人工修订题干为空")
        if (fixed_type in ("单选题", "多选题")
                and not mechfix.has_complete_choice_options(
                    fixed_body, known_choice=True)):
            raise ValueError(f"{row['path']} 的人工修订仍不是完整 A-D 选项")
        applied_overrides.append(row["path"])
        if write:
            filestore.update_question(
                row["id"], fixed_body, row["solution"],
                fixed_type, row["source"],
                row["difficulty"], row["tags"])
    return {
        "collection": collection,
        "question_count": len(rows),
        "changed_count": len(changed),
        "subquestion_changed_count": len(subquestion_changed),
        "subquestion_changed": subquestion_changed,
        "unchanged_choice_count": len(skipped),
        "needs_review_count": len(review_rows),
        "needs_review": review_rows,
        "override_count": len(applied_overrides),
        "overrides": applied_overrides,
        "changed": changed,
        "write": write,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="规范化试卷选择题标签与解答题小问排版")
    parser.add_argument("collection", help="题库相对路径，如 高考卷/2025")
    parser.add_argument("--overrides", type=Path,
                        help="逐题人工修订 JSON；写入前校验原正文 SHA-256")
    parser.add_argument("--confirm-write", action="store_true")
    args = parser.parse_args()
    overrides = None
    if args.overrides:
        overrides = json.loads(args.overrides.read_text(encoding="utf-8"))
        if not isinstance(overrides, list):
            parser.error("--overrides 必须是 JSON 数组")
    result = normalize_collection(
        args.collection, write=args.confirm_write, overrides=overrides)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
