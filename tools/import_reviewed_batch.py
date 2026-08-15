"""确认导入已经人工审核过的批次组。

不做自动判断或内容修改，只把转换快照中的默认校对结果完整提交给本机 ``/import``。
入库前再次拒绝题号断档和默认查重命中；必须显式传 ``--confirm-write``。
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import re
import sys
from pathlib import Path, PurePosixPath

import requests

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import app
import config
import dedup


_TOKEN_RE = re.compile(r'<meta\s+name="csrf-token"\s+content="([^"]+)"')


def _token(session: requests.Session, base_url: str) -> str:
    response = session.get(f"{base_url}/api/write-token", timeout=15)
    if response.ok:
        token = response.json().get("token")
        if isinstance(token, str) and token:
            return token
    # 兼容尚未升级轻量接口的旧后端。
    response = session.get(f"{base_url}/import", timeout=60)
    response.raise_for_status()
    match = _TOKEN_RE.search(response.text)
    if not match:
        raise RuntimeError("无法读取本机写入令牌")
    return match.group(1)


def _row_by_selector(rows: list[dict], override: dict) -> dict:
    if "idx" in override:
        found = [row for row in rows if str(row["idx"]) == str(override["idx"])]
    else:
        found = [row for row in rows if row.get("number") == override.get("number")]
    if len(found) != 1:
        raise ValueError(f"人工修订目标不唯一或不存在：{override}")
    return found[0]


def _apply_review_overrides(rows: list[dict], gid: int,
                            overrides: list[dict] | None) -> list[dict]:
    """应用经正文哈希保护的删、改、插入；用于 OCR 已无法恢复的少数题。"""
    fixed = [dict(row) for row in rows]
    relevant = [item for item in overrides or [] if int(item.get("gid", -1)) == gid]
    for serial, override in enumerate(relevant):
        operation = str(override.get("operation") or "replace")
        if operation == "insert":
            selector = {
                key.removeprefix("after_"): value
                for key, value in override.items()
                if key in {"after_idx", "after_number"}
            }
            reference = _row_by_selector(fixed, selector)
            actual = hashlib.sha256(reference["body"].encode("utf-8")).hexdigest()
            if actual != override.get("expected_reference_body_sha256"):
                raise ValueError("插题参考题干已变化，拒绝套用人工修订")
            number = int(override["number"])
            if any(row.get("number") == number for row in fixed):
                raise ValueError(f"待插入第 {number} 题已经存在")
            pos = fixed.index(reference) + 1
            next_idx = max(
                (int(row["idx"]) for row in fixed
                 if str(row.get("idx", "")).isdigit()),
                default=-1,
            ) + 1
            fixed.insert(pos, {
                # /import 路由把 idx 解析成整数；字符串临时索引会让插入题在 POST
                # 解析时被静默忽略。取本组现有最大数字索引加一，题号仍由 number
                # 字段决定，因此插入位置与文件名互不耦合。
                "idx": next_idx,
                "body": str(override.get("body") or "").strip(),
                "solution": str(override.get("solution") or "").strip(),
                "type": str(override.get("type") or "解答题"),
                "dup": False,
                "number": number,
            })
            continue

        target = _row_by_selector(fixed, override)
        actual = hashlib.sha256(target["body"].encode("utf-8")).hexdigest()
        if actual != override.get("expected_body_sha256"):
            raise ValueError("目标题干已变化，拒绝套用人工修订")
        if operation == "drop":
            fixed.remove(target)
            continue
        if operation != "replace":
            raise ValueError(f"不支持的人工修订操作：{operation}")
        if "body" in override:
            target["body"] = str(override["body"]).strip()
        if "body_prefix" in override:
            target["body"] = (str(override["body_prefix"]).strip()
                              + "\n\n" + target["body"].lstrip())
        if "solution" in override:
            target["solution"] = str(override["solution"]).strip()
        if "type" in override:
            target["type"] = str(override["type"])
        if "new_number" in override:
            target["number"] = int(override["new_number"])
    if any(not row["body"] for row in fixed):
        raise ValueError("人工修订产生了空题干")
    return fixed


def _missing_numbers(rows: list[dict]) -> list[int]:
    numbers = sorted({row.get("number") for row in rows
                      if isinstance(row.get("number"), int)})
    if not numbers:
        return []
    return [number for number in range(numbers[0], numbers[-1] + 1)
            if number not in numbers]


def import_groups(base_url: str, batch_id: str, gids: list[int],
                  overrides: list[dict] | None = None,
                  *, allow_missing: bool = False,
                  allow_duplicates: bool = False) -> list[dict]:
    store = json.loads(config.TASKS_PATH.read_text(encoding="utf-8"))
    entry = store.get("batch", {}).get(batch_id)
    if not entry:
        raise ValueError("批次快照不存在")
    batch = entry["payload"]
    by_gid = {int(group["gid"]): group for group in batch["groups"]}
    session = requests.Session()
    token = _token(session, base_url)
    results = []
    for gid in gids:
        group = by_gid.get(gid)
        if not group or group.get("status") != "done" or not group.get("md"):
            raise ValueError(f"第 {gid} 组尚未转换完成")
        # 归档批量导入只拒绝同卷内部重复，并不以跨卷指纹决定是否入库；这里传入
        # 空集合可避免每导一卷就重新扫描一次整座题库。目标文件夹仍在下方独立检查。
        preview, _folders, _missing = app._build_import_preview(
            group["md"], existing_fps=set(), all_cols=[])
        preview = _apply_review_overrides(preview, gid, overrides)
        missing = _missing_numbers(preview)
        if missing and not allow_missing:
            raise ValueError(f"{group['filename']} 仍缺题号：{missing}")
        # 历年卷归档允许不同试卷共用同一道题（文/理卷很常见），全库指纹命中不能
        # 阻止入库。真正危险的是同卷内部重复，或同名试卷文件夹已经有题——前者是
        # 识别错误，后者是重复提交/覆盖人工校订，两者都必须拒绝。
        fingerprints = [dedup.fingerprint(row["body"]) for row in preview]
        if (not allow_duplicates
                and any(count > 1 for count in Counter(fingerprints).values())):
            raise ValueError(f"{group['filename']} 同一试卷内出现重复题")
        number_counts = Counter(row.get("number") for row in preview)
        if (not allow_duplicates
                and any(number is not None and count > 1
                        for number, count in number_counts.items())):
            raise ValueError(f"{group['filename']} 同一试卷内出现重复题号")
        folder_parts = [batch.get("target_parent_id") or "",
                        batch.get("pack_folder_name") or "",
                        Path(group["filename"]).stem]
        folder_id = "/".join(part.strip("/") for part in folder_parts if part)
        target = config.BANK_DIR / PurePosixPath(folder_id)
        if target.is_dir() and any(target.glob("*.md")):
            raise ValueError(f"{group['filename']} 的目标试卷文件夹已有题目，拒绝覆盖")
        chosen = preview
        data: list[tuple[str, str]] = [
            ("action", "confirm"),
            ("batch_id", batch_id),
            ("batch_gid", str(gid)),
            ("job_id", str(group.get("job_id") or "")),
            ("batch_source", Path(group["filename"]).stem),
            # 历年卷已经用 source 字段记录卷名，不再额外制造同名标签。
            ("tags", ""),
            ("keep_original", "1"),
        ]
        for row in chosen:
            idx = str(row["idx"])
            data.extend([
                ("keep", idx),
                (f"body_{idx}", row["body"]),
                (f"solution_{idx}", row["solution"]),
                (f"type_{idx}", row["type"]),
                (f"num_{idx}", str(row["number"] or "")),
            ])
        response = session.post(
            f"{base_url}/import", data=data,
            headers={"X-CSRF-Token": token}, timeout=120,
            allow_redirects=False)
        if response.status_code not in (302, 303):
            raise RuntimeError(
                f"{group['filename']} 入库失败：HTTP {response.status_code}")
        results.append({"gid": gid, "filename": group["filename"],
                        "imported": len(chosen)})
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="确认导入已审核的批次组")
    parser.add_argument("batch_id")
    parser.add_argument("--group", type=int, action="append", required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:5000")
    parser.add_argument("--overrides", type=Path,
                        help="经正文 SHA-256 保护的人工修订 JSON")
    parser.add_argument("--confirm-write", action="store_true")
    args = parser.parse_args()
    if not args.confirm_write:
        parser.error("该操作会写入真实题库；确认后请显式传 --confirm-write")
    overrides = None
    if args.overrides:
        overrides = json.loads(args.overrides.read_text(encoding="utf-8"))
        if not isinstance(overrides, list):
            parser.error("--overrides 必须是 JSON 数组")
    rows = import_groups(
        args.base_url.rstrip("/"), args.batch_id, args.group, overrides)
    print(json.dumps(rows, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, requests.RequestException, RuntimeError, ValueError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        raise SystemExit(1)
