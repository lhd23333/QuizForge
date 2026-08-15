"""为一段年份的高考试卷生成可复现的分层抽检清单。

每年按 ``floor(卷数 * 比例 + 0.5)`` 取整；先覆盖全国卷、地方卷，并按年份奇偶
交替优先文/理卷，名额足够时再覆盖春季卷。剩余名额按“年份 + 文件名”的稳定哈希
补齐，因此同一批源文件重复运行会得到完全相同的清单。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from pathlib import Path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _score(year: int, name: str) -> str:
    return hashlib.sha256(f"{year}\0{name}".encode("utf-8")).hexdigest()


def _strata(name: str) -> list[str]:
    result = ["全国卷" if "全国" in name else "地方卷"]
    if "文卷" in name or "文科" in name:
        result.append("文科卷")
    if "理卷" in name or "理科" in name:
        result.append("理科卷")
    if "春季" in name:
        result.append("春季卷")
    return result


def choose_year(year: int, pdfs: list[Path], rate: float) -> list[Path]:
    quota = max(1, min(len(pdfs), int(len(pdfs) * rate + 0.5)))
    ordered = sorted(pdfs, key=lambda path: (_score(year, path.name), path.name))
    selected: list[Path] = []
    priorities = ["全国卷", "地方卷"]
    priorities.extend(["理科卷", "文科卷"] if year % 2 else ["文科卷", "理科卷"])
    priorities.append("春季卷")
    for stratum in priorities:
        if len(selected) >= quota:
            break
        candidate = next(
            (path for path in ordered
             if path not in selected and stratum in _strata(path.name)),
            None,
        )
        if candidate is not None:
            selected.append(candidate)
    selected.extend(path for path in ordered
                    if path not in selected)  # 稳定哈希补足剩余名额
    return sorted(selected[:quota], key=lambda path: path.name)


def build_plan(source_root: Path, start: int, end: int, rate: float) -> dict:
    years = []
    for year in range(start, end + 1):
        year_dir = source_root / str(year)
        pdfs = sorted(year_dir.glob("*.pdf"))
        if not pdfs:
            raise ValueError(f"年份目录没有 PDF：{year_dir}")
        selected = choose_year(year, pdfs, rate)
        years.append({
            "year": year,
            "source_count": len(pdfs),
            "sample_count": len(selected),
            "samples": [{
                "file": path.name,
                "bytes": path.stat().st_size,
                "sha256": _sha256(path),
                "strata": _strata(path.name),
            } for path in selected],
        })
    source_count = sum(row["source_count"] for row in years)
    sample_count = sum(row["sample_count"] for row in years)
    return {
        "schema": 1,
        "range": f"{start}-{end}",
        "requested_rate": rate,
        "source_count": source_count,
        "sample_count": sample_count,
        "actual_rate": sample_count / source_count,
        "review_scope": "每卷首题、中段题、末题及全部图表题",
        "years": years,
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
    parser = argparse.ArgumentParser(description="生成历年试卷分层抽检清单")
    parser.add_argument("source_root", type=Path)
    parser.add_argument("--start", type=int, required=True)
    parser.add_argument("--end", type=int, required=True)
    parser.add_argument("--rate", type=float, default=0.2)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 0 < args.rate <= 1:
        parser.error("--rate 必须在 (0, 1] 范围内")
    if args.start > args.end:
        parser.error("--start 不能大于 --end")
    plan = build_plan(args.source_root, args.start, args.end, args.rate)
    text = json.dumps(plan, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        _write_atomic(args.output, text)
    else:
        print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
