"""一次性数据修复：把被误判成「自定义分区」的题干挪回正文。

背景（2026-08-09）：MinerU 会把原卷题号提成 `## ` 标题行，而题号被
`$\\displaystyle N$` 包着 —— `importer.strip_leading_number` 的
`_STRIP_NUM_RE` 只认裸数字，剥不掉它。于是这行留在正文首行，被
`filestore._split_sections` 当成用户自定义分区标题，题干成了空字符串。
题卡（`app.py` 的 qbody 过滤器）与 PDF 导出（`exporter`）都只读 `body`，
两边一起空白 —— 内容其实一直完整躺在 `extra_sections` 里。

本脚本做的事，逐题：
  ① 认出正文首行那个「题号标题」（`## <题号>．【<出处>】<OCR 垃圾>`）；
  ② 把标题**整行删掉**，其后的内容升为题干；
  ③ 标题里的【出处】（`2016 北京, 18`）写进 frontmatter 的 `source`。

只改「题干为空且首个分区是题号标题」的题。带 `## 解析` 的、题干本来非空的、
首行标题不像题号的，一律跳过 —— 那些不是这个 bug。

默认 dry-run，只打印。加 --apply 才写盘。
"""

from __future__ import annotations

import argparse
import re
import sys

import config
import filestore

# 题号标题行里的题号：`$\displaystyle 13$．` / `13．` / `$\displaystyle 2.2$ `。
# 允许 `$\displaystyle ...$` 包裹是这个 bug 的全部成因，所以这里必须认它。
# 收尾符含空格（`$\displaystyle 2.2$ 验证定义`那种小节标题也要认出来，好把它
# 判成「没有题干」而不是误改），故用 `[.．、\s]`。
_NUM_HEAD_RE = re.compile(
    r"^(?:\$\\displaystyle\s*)?(?P<num>\d{1,3}(?:\.\d{1,3})?)\$?\s*[.．、\s]")

# 标题里的【出处】：`【$\displaystyle 2016$ 北京, $\displaystyle 18$】`
_SRC_RE = re.compile(r"【(?P<src>[^】]*)】")

# 标题行尾常粘的 OCR 垃圾：`题号位置: 15 16 17 18`（blocksplit.py:16 有记录）
_JUNK_RE = re.compile(r"题号位置\s*[:：].*$")


def _plain(text: str) -> str:
    """把 `$\\displaystyle X$` 拆掉，只留里面的字，供人读与写 source 用。"""
    text = re.sub(r"\$\\displaystyle\s*([^$]*?)\$", r"\1", text)
    return re.sub(r"\s+", " ", text).strip()


def _source_from_heading(heading: str) -> str:
    """标题里的【出处】→ 干净的 source 值；没有【】则返回空串。"""
    m = _SRC_RE.search(heading)
    if not m:
        return ""
    return _plain(m.group("src"))


# 正文开头的 `## ` 标题行（连同其后空行）。逐行剥而不是一次性正则：要的是
# 「开头连续的几行标题」，`(?m)^##.*$` 会把正文中段的标题也吃掉。
_LEAD_HEADING_RE = re.compile(r"\A[ \t]*#{1,6}[ \t]+[^\n]*\n*")


def _strip_lead_headings(text: str) -> str:
    """剥掉正文最前面**连续的**标题行，返回真正的题目内容。

    有些题被套了两层标题：外层是大节（`## 1.1 函数问题`，已被
    `_split_sections` 当成分区标题摘走），内层还有一层小节
    （`## 1.1.1 函数：导数计算基本功`），题目正文在那之后。只剥一层的话，
    题干会以一个突兀的章节标题开头。
    """
    prev = None
    while text != prev:
        prev = text
        text = _LEAD_HEADING_RE.sub("", text, count=1)
    return text


def classify(rec: dict) -> tuple[str, str]:
    """这道题属于哪一类，返回 (类别, 说明)。

    类别：
      fix      —— 正是这个 bug（题号标题 + 【出处】），可自动修
      review   —— 题干为空，但不像「题号标题吞题干」，要人工看
      skip     —— 不是这个 bug

    **判 fix 的关键是标题之下有没有真的题目内容**，不是标题长什么样。删掉
    标题行能不能修好这道题，只取决于下面还剩不剩东西：
      - 剩正文 → 就是本 bug（标题行吞掉了题干），删掉即可；
      - 什么都不剩、或只剩下一级 `## ` 章节标题 → 是**另一个** bug：切块时把
        章节标题当成了一道题。那种题删掉标题也变不出题干来，得人工决定删不删，
        脚本不动。

    标题里带不带【出处】（`【2016 北京, 18】`）只决定要不要改写 `source`：
    题号行才有出处，章节标题（`## $\\displaystyle 2.3$ 验证定义（3）数列`）没有，
    那时保留原 `source` 不动。
    """
    if (rec["body"] or "").strip():
        return "skip", "题干非空"
    if not rec["extra_sections"]:
        return "skip", "没有分区内容"
    if len(rec["extra_sections"]) != 1:
        return "review", f"有 {len(rec['extra_sections'])} 个分区，形态不符"
    heading, content = rec["extra_sections"][0]
    if heading == "解析":
        return "review", "首个分区是解析"
    if not _NUM_HEAD_RE.match(_plain(heading)):
        return "review", "首行标题不像题号"
    if not _strip_lead_headings(content).strip():
        return "review", "标题之下没有题目内容（切块时把章节标题当成了一道题）"
    return "fix", ""


def plan(rec: dict) -> dict:
    """算出这道题要怎么改，不落盘。"""
    heading, content = rec["extra_sections"][0]
    inner = re.findall(r"(?m)^#{1,6}[ \t]+(.*)$",
                       content[:len(content) - len(_strip_lead_headings(content))])
    return {
        "path": rec["path"],
        "id": rec["id"],
        "heading": heading,
        "inner_headings": inner,
        "new_body": _strip_lead_headings(content).strip(),
        "new_source": _source_from_heading(heading),
        "old_source": rec["source"],
    }


def apply_one(item: dict) -> None:
    """把一道题改掉：正文换成新题干，source 换成【出处】。

    走 filestore 的读写而不是自己拼文本：`_write_raw` 负责行尾归一
    （`normalize_newlines`，见那里关于 `\\r\\r\\n` 每存一轮翻倍的注释），
    frontmatter 的未知字段也靠 ruamel 往返原样保留。
    """
    path = config.BANK_DIR / item["path"]
    meta, _body = filestore._read_raw(path)
    if item["new_source"]:
        meta["source"] = item["new_source"]
    # 解析与其余分区都为空（classify 已保证只有一个分区且不是解析），
    # 所以新正文就是纯题干。
    full_body = filestore._join_sections(item["new_body"], "", [])
    filestore._write_raw(path, meta, full_body)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="真的写盘（默认只预演）")
    ap.add_argument("--limit", type=int, default=8,
                    help="预演时打印几道题的详情（默认 8）")
    args = ap.parse_args()

    recs = filestore._all_records()
    buckets: dict[str, list] = {"fix": [], "review": [], "skip": []}
    reasons: dict[str, str] = {}
    for rec in recs:
        kind, why = classify(rec)
        buckets[kind].append(rec)
        if kind != "fix":
            reasons[rec["path"]] = why

    items = [plan(r) for r in buckets["fix"]]
    empty_review = [r for r in buckets["review"]
                    if not (r["body"] or "").strip()]

    print(f"题库：{config.BANK_DIR}")
    print(f"共 {len(recs)} 题；可自动修 {len(items)} 题；"
          f"待人工核对 {len(empty_review)} 题；正常 {len(buckets['skip'])} 题")

    if items:
        print(f"\n=== 可自动修的 {len(items)} 题（前 {args.limit} 例）===")
        for it in items[:args.limit]:
            print(f"\n  {it['path']}")
            print(f"    删掉标题 : {_plain(it['heading'])[:78]}")
            for h in it["inner_headings"]:
                print(f"    并删内层 : {_plain(h)[:78]}")
            print(f"    新题干   : {_plain(it['new_body'])[:78]}")
            if it["new_source"]:
                print(f"    source   : {it['old_source']!r} → {it['new_source']!r}")
            else:
                print(f"    source   : 保持 {it['old_source']!r}（标题无【出处】）")

    if empty_review:
        print(f"\n=== 待人工核对的 {len(empty_review)} 题"
              f"（题干为空但不是本 bug，脚本不动）===")
        for r in empty_review:
            print(f"  {r['path']}")
            print(f"    原因：{reasons[r['path']]}")
            if r["extra_sections"]:
                print(f"    标题：{_plain(r['extra_sections'][0][0])[:66]}")

    if not args.apply:
        print("\n预演结束，未改动任何文件。确认无误后加 --apply 执行。")
        return 0

    done = 0
    failed = []
    for it in items:
        try:
            apply_one(it)
            done += 1
        except Exception as e:      # 单题失败不该中断整批
            failed.append((it["path"], repr(e)))
    print(f"\n已修复 {done} 题。")
    if failed:
        print(f"失败 {len(failed)} 题：")
        for p, e in failed:
            print(f"  {p}: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
