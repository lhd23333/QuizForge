"""离线切题回归：遍历留档 raw.md，检测正文覆盖率是否低于基线。"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import traceback

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import blocksplit  # noqa: E402
import config  # noqa: E402
import corpus  # noqa: E402

_EPS = 0.001


def _nonspace(text: str) -> int:
    return len("".join(text.split()))


def _image_only(raw: str) -> bool:
    for line in raw.splitlines():
        value = line.strip()
        if not value or value == "# 参考答案与解析":
            continue
        if not (value.startswith("![") and value.endswith(")")):
            return False
    return True


def measure(raw: str) -> dict:
    if _image_only(raw):
        return {"skipped": "image_only"}
    try:
        blocks, note = blocksplit.split_blocks_with_note(raw)
        paired = blocksplit.pair_blocks(blocks)
        stems = [block for block in blocks if block.zone == "stem"]
        total = _nonspace(raw)
        covered = sum(_nonspace(block.text) for block in blocks)
        return {
            "blocks": len(blocks),
            "stems": len(stems),
            "numbered": sum(block.number is not None for block in stems),
            "cover": round(covered / total, 4) if total else 0.0,
            "gaps": paired.number_gaps,
            "orphans": len(paired.orphan_solutions),
            "conflicts": len(paired.conflicts),
            "note": bool(note),
        }
    except Exception as exc:  # 一份坏语料不能阻断其余语料的体检
        return {"error": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc(limit=3)}


def sources():
    for archive, _meta in corpus.iter_archives():
        path = corpus.raw_md_of(archive)
        if path is None:
            continue
        try:
            yield archive.name, path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue


def main() -> int:
    parser = argparse.ArgumentParser(description="QuizForge 离线切题回归")
    parser.add_argument("--save", metavar="FILE", help="保存本次指标为基线")
    parser.add_argument("--baseline", metavar="FILE", help="与基线比较正文覆盖率")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    results = {name: measure(raw) for name, raw in sources()}
    if not results:
        print(f"[eval_split] {config.CORPUS_DIR} 下没有留档语料。")
        return 0
    bad = {name: row for name, row in results.items() if row.get("error")}
    ok = [row for row in results.values()
          if not row.get("error") and not row.get("skipped")]
    covers = sorted(row["cover"] for row in ok)
    print(f"[eval_split] 语料 {len(results)} 份，异常 {len(bad)} 份")
    if covers:
        print(f"  正文覆盖率：最低 {covers[0]}，中位 {covers[len(covers) // 2]}")
    if args.verbose:
        for name, row in sorted(results.items()):
            print(f"  {name}: {json.dumps(row, ensure_ascii=False)}")
    if args.save:
        path = pathlib.Path(args.save)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  基线已保存：{path}")
    regressions = []
    if args.baseline:
        baseline = json.loads(pathlib.Path(args.baseline).read_text(encoding="utf-8"))
        for name, current in results.items():
            old = baseline.get(name)
            if not old:
                continue
            if current.get("error") and not old.get("error"):
                regressions.append(f"{name} 新增异常：{current['error']}")
            elif (not current.get("skipped") and not old.get("skipped")
                  and current.get("cover", 0) < old.get("cover", 0) - _EPS):
                regressions.append(
                    f"{name} 正文覆盖率 {old['cover']} → {current['cover']}")
    for message in regressions:
        print(f"  [回退] {message}")
    return 1 if bad or regressions else 0


if __name__ == "__main__":
    raise SystemExit(main())
