from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import mechfix


SOLUTION_HEADING_RE = re.compile(r"(?m)^##\s*解析\s*$")
DISPLAYSTYLE_RE = re.compile(r"^\\displaystyle\s*")


def split_frontmatter(text: str) -> tuple[str, str]:
    """返回（frontmatter及分隔线，正文），找不到 frontmatter 时正文为全文。"""
    if not text.startswith("---\n"):
        return "", text
    marker = text.find("\n---\n", 4)
    if marker < 0:
        return "", text
    end = marker + len("\n---\n")
    return text[:end], text[end:]


def split_solution(body: str) -> tuple[str, str | None]:
    match = SOLUTION_HEADING_RE.search(body)
    if not match:
        return body, None
    return body[: match.start()], body[match.end() :]


def _is_escaped(text: str, index: int) -> bool:
    slash_count = 0
    index -= 1
    while index >= 0 and text[index] == "\\":
        slash_count += 1
        index -= 1
    return slash_count % 2 == 1


def _looks_like_repaired_close(text: str, index: int) -> bool:
    """识别 OCR 把公式结束美元写成 ``\\$`` 的高置信情况。"""
    if index + 1 >= len(text):
        return True
    following = text[index + 1]
    return following.isspace() or following in "，。；：？！,.!?;:)]）】》、"


def _find_close(text: str, start: int, delimiter: str) -> tuple[int, bool] | None:
    index = start
    while index < len(text):
        found = text.find(delimiter, index)
        if found < 0:
            return None
        if not _is_escaped(text, found):
            return found, False
        if delimiter == "$" and _looks_like_repaired_close(text, found):
            return found, True
        index = found + len(delimiter)
    return None


def _normalize_inner(inner: str) -> str:
    inner = inner.strip()
    if not inner:
        return ""
    inner = DISPLAYSTYLE_RE.sub("", inner, count=1).strip()
    return "\\displaystyle " + inner


def _looks_like_block_open(text: str, index: int) -> bool:
    """Recognize a real block-math opener without swallowing adjacent inline math."""
    if index == 0:
        return True
    previous = text[index - 1]
    return previous.isspace() or previous in "([{\uFF08\u3010\uFF1A\uFF0C,\u3002\uFF1B;\uFF01\uFF1F!?=+-*/^)]\uFF09\u3011\u3009"


def normalize_math(text: str, counters: Counter[str]) -> str:
    """统一所有可配对的美元公式为 ``$\\displaystyle ...$``。"""
    # 旧数据中相邻公式常写成 ``$①$$③$``。先把结束美元和开始美元分开，
    # 否则扫描器会把它们误认成块公式。
    out: list[str] = []
    index = 0
    while index < len(text):
        if (text.startswith("$$", index)
                and not _is_escaped(text, index)
                and _looks_like_block_open(text, index)):
            close = _find_close(text, index + 2, "$$")
            if close is None:
                counters["unmatched_block_dollar"] += 1
                out.append(text[index:])
                break
            end, repaired = close
            inner = text[index + 2 : end]
            normalized = _normalize_inner(inner)
            if normalized:
                out.append("$" + normalized + "$")
                counters["block_to_inline"] += 1
                if "\\displaystyle" in inner:
                    counters["displaystyle_rewritten"] += 1
            else:
                out.append(text[index : end + 2])
                counters["empty_math_kept"] += 1
            index = end + 2
            continue
        if text[index] == "$" and not _is_escaped(text, index):
            close = _find_close(text, index + 1, "$")
            if close is None:
                counters["unmatched_inline_dollar"] += 1
                out.append(text[index:])
                break
            end, repaired = close
            inner = text[index + 1 : end]
            if repaired:
                # 仅去除被 OCR 错加在公式结束美元前的反斜杠。
                if inner.endswith("\\"):
                    inner = inner[:-1]
                counters["repaired_escaped_closing_dollar"] += 1
            normalized = _normalize_inner(inner)
            if normalized:
                out.append("$" + normalized + "$")
                counters["inline_normalized"] += 1
                if "\\displaystyle" in inner:
                    counters["displaystyle_rewritten"] += 1
            else:
                out.append(text[index : end + 1])
                counters["empty_math_kept"] += 1
            index = end + 1
            continue
        out.append(text[index])
        index += 1
    # Separate adjacent inline formulas so their closing/opening delimiters do not
    # look like a block-math marker on later passes.
    return re.sub(r"(?<=\$)\$(?=\\displaystyle)", " $", "".join(out))


def _protect_math_spans(text: str) -> tuple[str, dict[str, str]]:
    """用不含拉丁字母的令牌保护跨行公式，避免裸字母扫描破坏 cases/aligned。"""
    tokens: dict[str, str] = {}
    out: list[str] = []
    index = 0
    token_index = 0
    while index < len(text):
        delimiter = None
        if text[index] == "$" and not _is_escaped(text, index):
            delimiter = "$"
        if delimiter is None:
            out.append(text[index])
            index += 1
            continue
        close = _find_close(text, index + len(delimiter), delimiter)
        if close is None:
            out.append(text[index:])
            break
        end, _repaired = close
        token = f"\ue000{token_index}\ue001"
        token_index += 1
        tokens[token] = text[index : end + len(delimiter)]
        out.append(token)
        index = end + len(delimiter)
    return "".join(out), tokens


def _wrap_bare_letters_safe(text: str) -> str:
    protected, tokens = _protect_math_spans(text)
    protected = mechfix.wrap_bare_letters(protected)
    # Also cover explicit option prose such as ``A item`` / ``A option``.
    protected = re.sub(
        r"(?<![A-Za-z])([ABCD])(?=\s*(?:\u9879|\u9009\u9879))",
        r"$\\displaystyle \1$",
        protected,
    )
    for token, value in tokens.items():
        protected = protected.replace(token, value)
    return protected


def normalize_segment(text: str, counters: Counter[str]) -> str:
    before = text
    text = normalize_math(text, counters)
    # 公式已先统一，再用令牌保护跨行公式，避免 cases/aligned 内的变量被再次切碎。
    text = _wrap_bare_letters_safe(text)
    if text != before:
        counters["segments_changed"] += 1
    return text


def normalize_document(text: str) -> tuple[str, Counter[str]]:
    frontmatter, body = split_frontmatter(text)
    counters: Counter[str] = Counter()
    question, solution = split_solution(body)
    question = normalize_segment(question, counters)
    if solution is None:
        new_body = question
    else:
        solution = normalize_segment(solution, counters)
        new_body = body[: len(body) - len(solution) - len("## 解析\n")]
        # 以上切片依赖标准标题长度，不适用于标题后的 CRLF；这里用原始分段重组更可靠。
        heading = SOLUTION_HEADING_RE.search(body)
        assert heading is not None
        new_body = question + body[heading.start() : heading.end()] + solution
    return frontmatter + new_body, counters


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def iter_markdown(root: Path):
    yield from sorted(root.rglob("*.md"))


def make_backup(root: Path, backup: Path) -> dict:
    if backup.exists():
        raise FileExistsError(f"备份目录已存在，拒绝覆盖：{backup}")
    shutil.copytree(root, backup)
    source_files = list(iter_markdown(root))
    backup_files = list(iter_markdown(backup))
    source_rel = {p.relative_to(root).as_posix() for p in source_files}
    backup_rel = {p.relative_to(backup).as_posix() for p in backup_files}
    if source_rel != backup_rel:
        raise RuntimeError("备份文件清单与源题库不一致")
    return {
        "backup": str(backup),
        "file_count": len(backup_files),
        "sha256_manifest": {
            rel: sha256(backup / rel) for rel in sorted(backup_rel)
        },
    }


def process(root: Path, apply: bool, backup: Path | None) -> dict:
    files = list(iter_markdown(root))
    total = Counter()
    changed: list[dict] = []
    for path in files:
        raw = path.read_bytes()
        newline = b"\r\n" if b"\r\n" in raw else b"\n"
        text = raw.decode("utf-8")
        # ??? LF ?????????? CRLF ? frontmatter/???????
        # ????? CRLF ????? CRCRLF???????????????
        logical_text = text.replace("\r\n", "\n")
        normalized, counters = normalize_document(logical_text)
        if newline == b"\r\n":
            normalized = normalized.replace("\n", "\r\n")
        total.update(counters)
        if normalized != text:
            rel = path.relative_to(root).as_posix()
            changed.append({
                "path": rel,
                "before_sha256": hashlib.sha256(raw).hexdigest(),
                "after_sha256": hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
                "bytes_before": len(raw),
                "bytes_after": len(normalized.encode("utf-8")),
                "counters": dict(counters),
            })
            if apply:
                fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
                try:
                    with os.fdopen(fd, "wb") as stream:
                        stream.write(normalized.encode("utf-8"))
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temp_name, path)
                finally:
                    if os.path.exists(temp_name):
                        os.unlink(temp_name)
    return {
        "root": str(root),
        "file_count": len(files),
        "changed_count": len(changed),
        "counters": dict(total),
        "changed": changed,
        "applied": apply,
        "backup": str(backup) if backup else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="批量统一高考题公式与选项字母格式")
    parser.add_argument("root", type=Path)
    parser.add_argument("--apply", action="store_true", help="创建备份后写回题目")
    parser.add_argument("--backup", type=Path, help="备份目录；--apply 时必填")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    if not root.is_dir():
        parser.error(f"题库目录不存在：{root}")
    if args.apply and not args.backup:
        parser.error("--apply 必须同时指定 --backup")
    if args.apply:
        backup_info = make_backup(root, args.backup.resolve())
    else:
        backup_info = None
    result = process(root, args.apply, args.backup.resolve() if args.backup else None)
    result["backup_info"] = backup_info
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "file_count": result["file_count"],
        "changed_count": result["changed_count"],
        "counters": result["counters"],
        "applied": result["applied"],
        "report": str(args.report.resolve()),
        "backup": str(args.backup.resolve()) if args.backup else None,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

