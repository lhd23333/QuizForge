"""把一个年份的 PDF 提交给本机 QuizForge 批量转换。

该工具只负责“提交并等待转换”，默认不自动入库。调用方应先审核看板结果，再决定
是否导入；这样真实题库不会因为脚本误判而被直接写入。OCR 属于付费外部调用，必须
显式传 ``--confirm-paid``。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

import requests


_TOKEN_RE = re.compile(r'<meta\s+name="csrf-token"\s+content="([^"]+)"')


def _csrf(session: requests.Session, base_url: str) -> str:
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
        raise RuntimeError("后端未提供写入令牌，请确认后端版本已更新")
    return match.group(1)


def submit_year(base_url: str, source_year: Path, parent: str,
                *, engine: str = "block", block_mode: str = "no_ai",
                only: set[str] | None = None) -> dict:
    pdfs = sorted(source_year.glob("*.pdf"))
    if only:
        pdfs = [path for path in pdfs if path.stem in only or path.name in only]
    if not pdfs:
        raise ValueError(f"年份目录没有 PDF：{source_year}")
    session = requests.Session()
    token = _csrf(session, base_url)
    files = []
    handles = []
    try:
        for index, path in enumerate(pdfs):
            handle = path.open("rb")
            handles.append(handle)
            files.append((f"groups[{index}][file]",
                          (path.name, handle, "application/pdf")))
        data = {
            "target_parent_id": parent,
            "pack_folder": "1",
            "pack_folder_name": source_year.name,
            "engine": engine,
            "block_mode": block_mode,
        }
        response = session.post(
            f"{base_url}/batch-convert/create", data=data, files=files,
            headers={"X-CSRF-Token": token}, timeout=300)
        payload = response.json()
        if not response.ok or not payload.get("ok"):
            raise RuntimeError(payload.get("error") or f"提交失败：HTTP {response.status_code}")
    finally:
        for handle in handles:
            handle.close()

    batch_id = payload["batch_id"]
    while True:
        status_response = session.get(
            f"{base_url}/batch-convert/status/{batch_id}", timeout=30)
        status_response.raise_for_status()
        status = status_response.json()
        states = {g.get("status") for g in status.get("groups", [])}
        print(json.dumps({"batch_id": batch_id, "status": status.get("status"),
                          "states": sorted(states)}, ensure_ascii=False), flush=True)
        if status.get("status") in ("done", "error"):
            status["batch_id"] = batch_id
            return status
        time.sleep(5)


def main() -> int:
    parser = argparse.ArgumentParser(description="提交一个年份的高考试卷转换任务")
    parser.add_argument("source_year", type=Path)
    parser.add_argument("--parent", required=True, help="QuizForge 目标父文件夹 ID")
    parser.add_argument("--base-url", default="http://127.0.0.1:5000")
    parser.add_argument("--engine", choices=("block", "whole"), default="block")
    parser.add_argument("--block-mode", choices=("no_ai", "all_ai"), default="no_ai")
    parser.add_argument("--only", action="append", default=[],
                        help="只提交指定文件名或 stem，可重复使用")
    parser.add_argument("--confirm-paid", action="store_true")
    args = parser.parse_args()
    if not args.confirm_paid:
        parser.error("OCR 会调用付费外部服务；确认后请显式传 --confirm-paid")
    status = submit_year(args.base_url.rstrip("/"), args.source_year, args.parent,
                         engine=args.engine, block_mode=args.block_mode,
                         only=set(args.only) or None)
    errors = [g for g in status.get("groups", []) if g.get("status") == "error"]
    print(json.dumps(status, ensure_ascii=False, indent=2))
    return 1 if errors else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, requests.RequestException, RuntimeError, ValueError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        raise SystemExit(1)
