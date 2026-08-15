"""安全修复已入库历年卷中可机械确认的识别瑕疵。

默认只扫描并输出统计；只有显式传 ``--apply --confirm-write`` 才写入。正式写入前
完整备份指定年份目录，随后逐文件原子替换，并在备份目录留下哈希清单。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import config
import mechfix


_SOLUTION_TYPE_RE = re.compile(
    r"^type:\s*['\"]?解答题['\"]?\s*$", re.M)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _atomic_write(path: Path, data: bytes) -> None:
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def _default_backup_root(bank_root: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    # ``.../Obsidian/QuizForge/高考卷`` → ``.../Obsidian/QuizForge_backups/...``
    obsidian_root = bank_root.parent.parent
    return obsidian_root / "QuizForge_backups" / f"高考卷_sub修复前_{stamp}"


def _normalize_file_text(text: str, *, fix_subquestions: bool,
                         fix_constraints: bool) -> str:
    fixed = mechfix.normalize_html_subscripts(text)
    fixed = mechfix.normalize_html_superscripts(fixed)
    fixed = mechfix.normalize_intrusive_column_text(fixed)
    if fix_constraints:
        fixed = mechfix.normalize_misplaced_constraints(fixed)
    if not fix_subquestions or not _SOLUTION_TYPE_RE.search(fixed):
        return fixed
    marker = "\n---\n"
    if marker not in fixed:
        return fixed
    frontmatter, body = fixed.split(marker, 1)
    leading = body[:len(body) - len(body.lstrip("\n"))]
    trailing = body[len(body.rstrip("\n")):]
    core = body.strip("\n")
    normalized = mechfix.normalize_subquestion_layout(core)
    if normalized == core:
        return fixed
    return frontmatter + marker + leading + normalized + trailing


def scan(bank_root: Path, years: list[str], *,
         fix_subquestions: bool = False,
         fix_constraints: bool = False) -> tuple[list[dict], list[str]]:
    rows = []
    missing = []
    for year in years:
        year_dir = bank_root / year
        if not year_dir.is_dir():
            missing.append(year)
            continue
        for path in sorted(year_dir.rglob("*.md")):
            before = path.read_bytes()
            text = before.decode("utf-8")
            fixed = _normalize_file_text(
                text, fix_subquestions=fix_subquestions,
                fix_constraints=fix_constraints)
            after = fixed.encode("utf-8")
            if after == before:
                continue
            if "<sub" in fixed.lower() or "</sub" in fixed.lower():
                raise ValueError(f"规范化后仍残留 <sub>：{path}")
            rows.append({
                "path": path,
                "relative": path.relative_to(bank_root).as_posix(),
                "before": before,
                "after": after,
                "before_sha256": _sha256_bytes(before),
                "after_sha256": _sha256_bytes(after),
            })
    return rows, missing


def apply_changes(bank_root: Path, years: list[str], rows: list[dict],
                  backup_root: Path) -> dict:
    if backup_root.exists():
        raise FileExistsError(f"备份目录已存在，拒绝覆盖：{backup_root}")
    backup_root.parent.mkdir(parents=True, exist_ok=True)
    backup_root.mkdir()
    try:
        for year in years:
            source = bank_root / year
            if source.is_dir():
                shutil.copytree(source, backup_root / year)

        manifest = {
            "schema": 1,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "bank_root": str(bank_root.resolve()),
            "years": years,
            "changed_files": [{
                "path": row["relative"],
                "before_sha256": row["before_sha256"],
                "after_sha256": row["after_sha256"],
            } for row in rows],
        }
        (backup_root / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8", newline="\n")

        for row in rows:
            path = row["path"]
            # 扫描与写入之间若被人工编辑，必须停下，不能覆盖校订。
            if _sha256_bytes(path.read_bytes()) != row["before_sha256"]:
                raise RuntimeError(f"文件在扫描后发生变化，拒绝覆盖：{path}")
            _atomic_write(path, row["after"])
    except Exception:
        # 已建备份时不自动删；即使中途失败，它仍是恢复依据。
        raise
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="修复已入库年份中的 MinerU <sub> 标签")
    parser.add_argument("--bank-root", type=Path,
                        default=config.BANK_DIR / "高考卷")
    parser.add_argument("--year", action="append", required=True)
    parser.add_argument("--backup-root", type=Path)
    parser.add_argument(
        "--fix-subquestions", action="store_true",
        help="同时修复因 <sub> 遮挡而未分段的解答题连续小问")
    parser.add_argument(
        "--fix-constraints", action="store_true",
        help="同时修复被分栏抽到最值提示后的单条约束")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-write", action="store_true")
    args = parser.parse_args()

    bank_root = args.bank_root.resolve()
    rows, missing = scan(
        bank_root, args.year, fix_subquestions=args.fix_subquestions,
        fix_constraints=args.fix_constraints)
    summary = {
        "bank_root": str(bank_root),
        "years": args.year,
        "missing_years": missing,
        "changed_file_count": len(rows),
        "changed_files": [row["relative"] for row in rows],
        "mode": "apply" if args.apply else "dry-run",
        "fix_subquestions": args.fix_subquestions,
        "fix_constraints": args.fix_constraints,
    }
    if missing:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 1
    if not args.apply:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    if not args.confirm_write:
        parser.error("写入真实题库前必须显式传 --confirm-write")
    if not rows:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0

    backup_root = (args.backup_root or _default_backup_root(bank_root)).resolve()
    manifest = apply_changes(bank_root, args.year, rows, backup_root)
    summary["backup_root"] = str(backup_root)
    summary["manifest_changed_file_count"] = len(manifest["changed_files"])
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
