"""在隔离目录启动桌面成品，验证设备码、错机拒绝和许可证导入。"""

from __future__ import annotations

import argparse
from datetime import date
import html
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

import requests


TOOLS_DIR = Path(__file__).resolve().parent
PROJECT_DIR = TOOLS_DIR.parent
sys.path.insert(0, str(PROJECT_DIR))
sys.path.insert(0, str(TOOLS_DIR))

import license_signer


_PORT_RE = re.compile(r"127\.0\.0\.1:(\d+)")
_CSRF_RE = re.compile(r'<meta\s+name="csrf-token"\s+content="([^"]+)"')
_DEVICE_RE = re.compile(
    r'id="license-device-id"[^>]*\bvalue="([^"]+)"', re.DOTALL
)


def _wait_for_server(log_path: Path, process: subprocess.Popen) -> str:
    deadline = time.monotonic() + 45
    last_log = ""
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"桌面成品提前退出，返回码 {process.returncode}")
        try:
            last_log = log_path.read_text(encoding="utf-8")
        except OSError:
            last_log = ""
        match = _PORT_RE.search(last_log)
        if match:
            base = f"http://127.0.0.1:{match.group(1)}"
            try:
                response = requests.get(base + "/healthz", timeout=1)
                if response.status_code == 200 and response.json().get("status") == "ok":
                    return base
            except (requests.RequestException, ValueError):
                pass
        time.sleep(0.2)
    raise RuntimeError(f"45 秒内没有发现桌面本地服务；日志尾部：{last_log[-500:]}")


def _page_values(text: str) -> tuple[str, str]:
    csrf_match = _CSRF_RE.search(text)
    device_match = _DEVICE_RE.search(text)
    if csrf_match is None or device_match is None:
        raise RuntimeError("设置页没有输出 CSRF 令牌或设备请求码")
    return html.unescape(csrf_match.group(1)), html.unescape(device_match.group(1))


def _other_device(device_id: str) -> str:
    chars = list(device_id)
    index = len("QFD1-")
    chars[index] = "B" if chars[index] != "B" else "A"
    return "".join(chars)


def _sign(private_key: Path, device_id: str) -> dict:
    args = argparse.Namespace(
        private_key=private_key,
        output=Path("unused.qflicense"),
        licensee="packaged-smoke",
        device_id=device_id,
        license_id="",
        edition="beta",
        issued="",
        not_before="",
        valid_days=license_signer.DEFAULT_VALID_DAYS,
        expires="",
        perpetual=False,
        updates_until="",
        feature=["export"],
        password_env="",
        no_password=True,
    )
    return license_signer.issue_license(args)


def _raw(document: dict) -> bytes:
    return (
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="QuizForge 设备绑定成品烟测")
    parser.add_argument("--exe", type=Path, required=True)
    parser.add_argument("--private-key", type=Path, required=True)
    args = parser.parse_args()
    exe = args.exe.resolve()
    private_key = args.private_key.resolve()
    if not exe.is_file() or not private_key.is_file():
        parser.error("桌面 EXE 或签发私钥不存在")

    with tempfile.TemporaryDirectory(prefix="quizforge-device-smoke-") as td:
        root = Path(td)
        data_dir = root / "data"
        bank_dir = root / "bank"
        bank_dir.mkdir()
        env = os.environ.copy()
        env["QUIZFORGE_DATA_DIR"] = str(data_dir)
        env["QUIZFORGE_BANK"] = str(bank_dir)
        process = subprocess.Popen(
            [str(exe)], cwd=exe.parent, env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            base = _wait_for_server(data_dir / "logs" / "quizforge.log", process)
            session = requests.Session()
            settings = session.get(base + "/settings", timeout=5)
            settings.raise_for_status()
            csrf, device_id = _page_values(settings.text)
            identity_path = data_dir / "device_identity.dat"
            if not identity_path.is_file():
                raise RuntimeError("桌面成品未保存 DPAPI 设备身份")
            if device_id.encode("ascii") in identity_path.read_bytes():
                raise RuntimeError("设备身份文件泄露了明文请求码")

            wrong = _sign(private_key, _other_device(device_id))
            wrong_response = session.post(
                base + "/settings/license",
                files={"license_file": ("wrong.qflicense", _raw(wrong))},
                headers={"X-CSRF-Token": csrf}, timeout=10,
            )
            wrong_response.raise_for_status()
            if "不适用于本机" not in wrong_response.text:
                raise RuntimeError("绑定到另一设备的许可证没有被拒绝")

            settings = session.get(base + "/settings", timeout=5)
            csrf, _ = _page_values(settings.text)
            correct = _sign(private_key, device_id)
            correct_response = session.post(
                base + "/settings/license",
                files={"license_file": ("correct.qflicense", _raw(correct))},
                headers={"X-CSRF-Token": csrf}, timeout=10,
            )
            correct_response.raise_for_status()
            if "许可证已导入" not in correct_response.text:
                raise RuntimeError("正确设备许可证未成功导入")
            payload = correct["payload"]
            valid_days = (
                date.fromisoformat(payload["expires_at"])
                - date.fromisoformat(payload["not_before"])
            ).days + 1
            if correct["schema"] != 2 or valid_days != 7:
                raise RuntimeError("成品许可证不是 schema 2 或默认时长不是 7 天")
            print("[OK] packaged device identity: DPAPI encrypted")
            print("[OK] wrong-device license: rejected")
            print("[OK] matching license: imported, schema 2, valid 7 calendar days")
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
