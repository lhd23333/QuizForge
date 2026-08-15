"""给已入库试卷补入经原题正文哈希保护的少量缺题。"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path, PurePosixPath

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import filestore
import mechfix


def _folder_of(row: dict) -> str:
    return PurePosixPath(row["path"]).parent.as_posix()


def validate_patch(rows: list[dict], collection: str, patch: dict) -> dict:
    folder = str(patch.get("folder") or "").strip("/")
    if not folder or not (folder == collection or folder.startswith(collection + "/")):
        raise ValueError(f"补题目录越界：{folder}")
    number = int(patch["number"])
    folder_rows = [row for row in rows if _folder_of(row) == folder]
    if any(row.get("number") == number for row in folder_rows):
        raise ValueError(f"{folder} 已有第 {number} 题")
    reference_number = int(patch["reference_number"])
    references = [row for row in folder_rows
                  if row.get("number") == reference_number]
    if len(references) != 1:
        raise ValueError(f"{folder} 的参考题号不唯一：{reference_number}")
    reference = references[0]
    actual = hashlib.sha256(reference["body"].encode("utf-8")).hexdigest()
    if actual != patch.get("expected_reference_body_sha256"):
        raise ValueError(f"{reference['path']} 已变化，拒绝补题")
    body = str(patch.get("body") or "").strip()
    qtype = str(patch.get("type") or "解答题")
    if not body:
        raise ValueError("补入题干为空")
    if (qtype in ("单选题", "多选题")
            and not mechfix.has_complete_choice_options(body, known_choice=True)):
        raise ValueError(f"待补第 {number} 题的 A-D 选项不完整")
    return {"folder": folder, "number": number, "body": body,
            "solution": str(patch.get("solution") or "").strip(),
            "type": qtype, "source": str(patch.get("source") or Path(folder).name)}


def apply_patches(collection: str, patches: list[dict], *, write: bool) -> dict:
    rows = filestore.list_questions(collection=collection)
    validated = [validate_patch(rows, collection, patch) for patch in patches]
    created = []
    if write:
        for patch in validated:
            qid = filestore.create_question(
                patch["body"], patch["solution"], patch["type"],
                patch["source"], folder=patch["folder"],
                number=patch["number"],
            )
            created.append({"id": qid, "folder": patch["folder"],
                            "number": patch["number"]})
    return {"collection": collection, "patch_count": len(validated),
            "patches": [{"folder": row["folder"], "number": row["number"]}
                        for row in validated],
            "created": created, "write": write}


def main() -> int:
    parser = argparse.ArgumentParser(description="补入已入库试卷中经校对确认的缺题")
    parser.add_argument("collection")
    parser.add_argument("patches", type=Path)
    parser.add_argument("--confirm-write", action="store_true")
    args = parser.parse_args()
    patches = json.loads(args.patches.read_text(encoding="utf-8"))
    if not isinstance(patches, list):
        parser.error("patches 必须是 JSON 数组")
    print(json.dumps(apply_patches(
        args.collection, patches, write=args.confirm_write),
        ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
