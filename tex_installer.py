"""固定版本 MiKTeX 的安全下载与当前用户静默安装。"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import threading
from urllib.parse import urlparse

import requests

import config


logger = logging.getLogger(__name__)

MIKTEX_VERSION = "25.12"
MIKTEX_FILENAME = f"basic-miktex-{MIKTEX_VERSION}-x64.exe"
MIKTEX_URL = (
    "https://miktex.org/download/ctan/systems/win32/miktex/setup/"
    f"windows-x64/{MIKTEX_FILENAME}"
)
MIKTEX_SHA256 = "14b42dd9f4b4a7813a8bfd69c8f99316c2888cc4ee26f631f397e163d85d6c62"
MIKTEX_SIZE = 148_882_184
# 2026-08-17 实测 Basic Installer 与官方 Setup Utility 均为 NotSigned。
# 安全要求不能退让，因此在上游提供有效 Authenticode 签名之前拒绝下载和执行。
MIKTEX_INSTALL_AVAILABLE = False
MIKTEX_INSTALL_BLOCK_REASON = (
    "MiKTeX 官方安装器未提供有效 Authenticode 签名，一键安装已安全关闭"
)
_ACTIVE_STATES = {"queued", "downloading", "verifying", "installing"}
_STATE_LOCK = threading.Lock()
_STATE: dict[str, object] = {
    "status": "idle",
    "downloaded": 0,
    "total": MIKTEX_SIZE,
    "message": "",
    "error": "",
}


class TexInstallError(RuntimeError):
    """下载、验签或安装未能安全完成。"""


def _set_state(**changes) -> None:
    with _STATE_LOCK:
        _STATE.update(changes)


def _resolve_tool(value: str) -> Path | None:
    configured = str(value or "").strip()
    if not configured:
        return None
    candidate = Path(configured)
    if candidate.is_absolute() or candidate.parent != Path("."):
        try:
            return candidate.resolve() if candidate.is_file() else None
        except OSError:
            return None
    found = shutil.which(configured)
    return Path(found).resolve() if found else None


def find_miktex_tool(name: str) -> Path | None:
    """动态寻找安装后的工具，避免进程启动时的 config 缓存造成假缺失。"""
    configured = config.XELATEX if name == "xelatex" else config.DVISVGM
    existing = _resolve_tool(configured)
    if existing is not None:
        return existing
    local_app_data = Path(os.environ.get("LOCALAPPDATA", ""))
    candidates = [
        local_app_data / "Programs" / "MiKTeX" / "miktex" / "bin" / "x64" / f"{name}.exe",
        Path(os.environ.get("PROGRAMFILES", r"C:\Program Files"))
        / "MiKTeX" / "miktex" / "bin" / "x64" / f"{name}.exe",
    ]
    for candidate in candidates:
        try:
            if candidate.is_file():
                return candidate.resolve()
        except OSError:
            continue
    return None


def snapshot() -> dict[str, object]:
    xelatex = find_miktex_tool("xelatex")
    with _STATE_LOCK:
        state = dict(_STATE)
    state.update({
        "installed": xelatex is not None,
        "xelatex": str(xelatex or ""),
        "version": MIKTEX_VERSION,
        "available": MIKTEX_INSTALL_AVAILABLE,
        "blocked_reason": (
            "" if MIKTEX_INSTALL_AVAILABLE else MIKTEX_INSTALL_BLOCK_REASON
        ),
    })
    return state


def _validate_https_chain(response) -> None:
    parsed = urlparse(MIKTEX_URL)
    if parsed.scheme != "https" or parsed.hostname != "miktex.org":
        raise TexInstallError("MiKTeX 固定下载地址配置无效")
    for item in [*response.history, response]:
        if urlparse(str(item.url)).scheme != "https":
            raise TexInstallError("MiKTeX 下载发生了非 HTTPS 跳转")


def download_installer(destination: Path, *, session=None,
                       progress=None) -> Path:
    """下载固定字节并同时校验长度与 SHA-256。"""
    client = session or requests.Session()
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(destination.suffix + ".part")
    digest = hashlib.sha256()
    downloaded = 0
    try:
        with client.get(
            MIKTEX_URL, stream=True, allow_redirects=True, timeout=(15, 60)
        ) as response:
            response.raise_for_status()
            _validate_https_chain(response)
            length = response.headers.get("Content-Length")
            if length and int(length) != MIKTEX_SIZE:
                raise TexInstallError("MiKTeX 安装器长度与固定版本不一致")
            with partial.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    downloaded += len(chunk)
                    if downloaded > MIKTEX_SIZE:
                        raise TexInstallError("MiKTeX 下载内容超过固定版本大小")
                    digest.update(chunk)
                    handle.write(chunk)
                    if progress is not None:
                        progress(downloaded, MIKTEX_SIZE)
                handle.flush()
                os.fsync(handle.fileno())
        if downloaded != MIKTEX_SIZE:
            raise TexInstallError("MiKTeX 安装器下载不完整")
        if digest.hexdigest().lower() != MIKTEX_SHA256:
            raise TexInstallError("MiKTeX 安装器 SHA-256 校验失败")
        os.replace(partial, destination)
        return destination
    except Exception:
        partial.unlink(missing_ok=True)
        raise


def verify_authenticode(installer: Path, *, runner=None) -> dict[str, str]:
    """使用 Windows Authenticode 验证固定哈希文件仍有有效发布签名。"""
    if os.name != "nt":
        raise TexInstallError("一键安装 MiKTeX 仅支持 Windows")
    powershell = (
        Path(os.environ.get("SystemRoot", r"C:\Windows"))
        / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    )
    command = (
        "$p=[Environment]::GetEnvironmentVariable('QUIZFORGE_SIGNATURE_PATH','Process');"
        "$s=Get-AuthenticodeSignature -LiteralPath $p;"
        "[pscustomobject]@{Status=[string]$s.Status;"
        "Subject=[string]$s.SignerCertificate.Subject;"
        "Thumbprint=[string]$s.SignerCertificate.Thumbprint}"
        "|ConvertTo-Json -Compress"
    )
    run = runner or subprocess.run
    environment = dict(os.environ)
    environment["QUIZFORGE_SIGNATURE_PATH"] = str(installer)
    result = run(
        [str(powershell), "-NoProfile", "-NonInteractive", "-Command",
         command],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=60, check=False, env=environment,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode != 0:
        raise TexInstallError("无法完成 MiKTeX Authenticode 校验")
    try:
        payload = json.loads(result.stdout.strip())
    except (TypeError, json.JSONDecodeError) as exc:
        raise TexInstallError("MiKTeX Authenticode 结果不可读") from exc
    if payload.get("Status") != "Valid" or not payload.get("Thumbprint"):
        raise TexInstallError("MiKTeX 安装器 Authenticode 签名无效")
    return {key: str(payload.get(key) or "") for key in ("Status", "Subject", "Thumbprint")}


def install_miktex(installer: Path, *, runner=None) -> Path:
    """按官方命令行选项安装到当前用户，不请求管理员权限。"""
    run = runner or subprocess.run
    result = run(
        [str(installer), "--unattended", "--private", "--package-set=basic"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=3600, check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode != 0:
        raise TexInstallError(f"MiKTeX 安装失败（退出码 {result.returncode}）")
    xelatex = find_miktex_tool("xelatex")
    if xelatex is None:
        raise TexInstallError("MiKTeX 安装完成，但未找到 XeLaTeX")
    config.XELATEX = str(xelatex)
    dvisvgm = find_miktex_tool("dvisvgm")
    if dvisvgm is not None:
        config.DVISVGM = str(dvisvgm)
    return xelatex


def _install_worker() -> None:
    download_dir = config.DATA_DIR / "downloads"
    installer = download_dir / MIKTEX_FILENAME
    partial = installer.with_suffix(installer.suffix + ".part")
    try:
        _set_state(status="downloading", downloaded=0, total=MIKTEX_SIZE,
                   message="正在下载 MiKTeX", error="")
        download_installer(
            installer,
            progress=lambda done, total: _set_state(downloaded=done, total=total),
        )
        _set_state(status="verifying", message="正在校验安装器签名")
        verify_authenticode(installer)
        _set_state(status="installing", message="正在安装 MiKTeX")
        xelatex = install_miktex(installer)
        _set_state(status="succeeded", downloaded=MIKTEX_SIZE,
                   message="MiKTeX 已安装，XeLaTeX 可以直接使用",
                   xelatex=str(xelatex), error="")
    except Exception as exc:
        logger.exception("MiKTeX 一键安装失败")
        message = str(exc) if isinstance(exc, TexInstallError) else "MiKTeX 安装过程中发生错误"
        _set_state(status="failed", message="安装未完成", error=message)
    finally:
        # 固定安装包只用于本次安装，成功或失败都不长期占用用户磁盘。
        partial.unlink(missing_ok=True)
        installer.unlink(missing_ok=True)


def start_install() -> dict[str, object]:
    if not MIKTEX_INSTALL_AVAILABLE:
        raise TexInstallError(MIKTEX_INSTALL_BLOCK_REASON)
    if os.name != "nt":
        raise TexInstallError("一键安装 MiKTeX 仅支持 Windows")
    if find_miktex_tool("xelatex") is not None:
        raise TexInstallError("本机已经可以使用 XeLaTeX")
    with _STATE_LOCK:
        if _STATE["status"] in _ACTIVE_STATES:
            raise TexInstallError("MiKTeX 正在下载或安装")
        _STATE.update(status="queued", downloaded=0, total=MIKTEX_SIZE,
                      message="准备下载 MiKTeX", error="")
    threading.Thread(target=_install_worker, name="quizforge-miktex-install",
                     daemon=True).start()
    return snapshot()
