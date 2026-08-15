"""对一批 PDF 运行 Doc2X，并统计 QuizForge 机械拆题结果。

API Key 只从 ``DOC2X_API_KEY`` 环境变量读取。输出按 PDF 内容哈希缓存；下游拆题
迭代可反复重放而不重复扣 Doc2X 页数，只有传 ``--force`` 才重新识别。
"""

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import blockpipe  # noqa: E402
import blocksplit  # noqa: E402
import doc2x_client  # noqa: E402
import optcheck  # noqa: E402


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()[:12]


def _evaluate(pdf: Path, out_root: Path, key: str, force: bool) -> dict:
    digest = _digest(pdf)
    out_dir = out_root / f"{pdf.stem}-{digest}"
    raw_path = out_dir / f"{pdf.stem}_raw.md"
    meta_path = out_dir / f"{pdf.stem}_doc2x.json"
    cached = raw_path.is_file() and meta_path.is_file() and not force
    if cached:
        raw = raw_path.read_text(encoding="utf-8")
    else:
        if not key:
            raise RuntimeError("缓存不存在，请通过 DOC2X_API_KEY 环境变量提供 API Key")
        result = doc2x_client.Doc2XClient(key).parse_pdf(
            pdf, extract_dir=out_dir)
        raw = result.markdown

    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    # 客户端的纯本地后处理迭代也要作用到旧缓存，避免为验证一个重排规则重新扣页。
    raw, repaired = doc2x_client._repair_figure_choice_order(
        raw, meta, out_dir / "images")
    if repaired:
        raw_path.write_text(raw, encoding="utf-8")
        meta["quizforge_repaired_figure_choices"] = repaired
        meta_path.write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    pages = meta.get("pages") or []
    notes: list[str] = []
    blocks = blockpipe.split_and_prep(raw, note_sink=notes.append)
    stem_blocks = [block for block in blocks if block.zone == "stem"]
    numbers = [block.number for block in stem_blocks
               if isinstance(block.number, int)]
    gaps = []
    if numbers:
        gaps = sorted(set(range(min(numbers), max(numbers) + 1)) - set(numbers))
    image_refs = re.findall(r"!\[[^\]]*\]\(\s*images/([^)\s]+)", raw)
    image_dir = out_dir / "images"
    images = list(image_dir.iterdir()) if image_dir.is_dir() else []
    result = {
        "file": str(pdf),
        "sha256_12": digest,
        "cached": cached,
        "pages": len(pages),
        "page_scores": [page.get("score") for page in pages],
        "raw_chars": len(raw),
        "raw_numbered_blocks": len([
            block for block in blocksplit.split_blocks(raw)
            if block.zone == "stem" and isinstance(block.number, int)
        ]),
        "prepared_stem_blocks": len(stem_blocks),
        "numbers": numbers,
        "number_gaps": gaps,
        "empty_options": [gap.describe() for gap in optcheck.find_empty_options(raw)],
        "image_refs": len(image_refs),
        "image_files": len(images),
        "notes": notes,
        "out_dir": str(out_dir),
    }
    (out_dir / "eval.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("--output", type=Path, default=ROOT / "tmp" / "doc2x_eval")
    parser.add_argument("--recursive", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    key = os.environ.get("DOC2X_API_KEY", "").strip()
    pattern = "**/*.pdf" if args.recursive else "*.pdf"
    files = sorted(args.input.glob(pattern)) if args.input.is_dir() else [args.input]
    files = [path.resolve() for path in files if path.is_file()]
    if not files:
        parser.error("没有找到 PDF")
    args.output.mkdir(parents=True, exist_ok=True)
    results = []
    for index, pdf in enumerate(files, 1):
        print(f"[{index}/{len(files)}] {pdf.name}", flush=True)
        try:
            result = _evaluate(pdf, args.output, key, args.force)
        except Exception as exc:  # 每份独立，单份失败不能挡住整套测试
            result = {"file": str(pdf), "error": f"{type(exc).__name__}: {exc}"}
        results.append(result)
        print(json.dumps(result, ensure_ascii=False), flush=True)
    summary = args.output / "summary.json"
    summary.write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"SUMMARY={summary.resolve()}", flush=True)
    return 1 if any("error" in item for item in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
