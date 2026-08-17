"""QuizForge 可选更新客户端。

更新只在用户主动操作后访问公开清单。安装包必须同时通过 HTTPS、大小、
SHA-256 与 Authenticode 签名校验，随后由独立 PowerShell 进程等待主程序退出、
原位覆盖并重启。题库、凭据和设备身份始终位于安装目录之外。
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import tempfile
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


MAX_MANIFEST_BYTES = 512 * 1024
MAX_INSTALLER_BYTES = 512 * 1024 * 1024
REQUEST_TIMEOUT_SECONDS = 8
DOWNLOAD_TIMEOUT_SECONDS = 300
_VERSION_RE = re.compile(
    r"^v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?$"
)
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_THUMBPRINT_RE = re.compile(r"^[0-9a-fA-F]{40}$")
_POWERSHELL = os.path.join(
    os.environ.get("SystemRoot", r"C:\Windows"),
    "System32", "WindowsPowerShell", "v1.0", "powershell.exe",
)


class UpdateCheckError(RuntimeError):
    """更新清单或安装包不符合安全约定。"""


class _NoPublishedUpdate(UpdateCheckError):
    """更新服务正常响应，但目前没有已发布的 Windows 版本。"""


def _https_url(value: object, *, field: str) -> str:
    url = str(value or "").strip()
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise UpdateCheckError(f"{field}必须使用 HTTPS 地址")
    if parsed.username or parsed.password or parsed.fragment:
        raise UpdateCheckError(f"{field}地址格式不安全")
    return url


def _version_key(value: object) -> tuple[int, int, int, int, str]:
    text = str(value or "").strip()
    match = _VERSION_RE.fullmatch(text)
    if not match:
        raise UpdateCheckError("更新清单版本号格式无效")
    major, minor, patch, prerelease = match.groups()
    # 正式版高于同号预发布版；预发布字符串仅用于稳定排序。
    return (
        int(major), int(minor), int(patch),
        0 if prerelease else 1, prerelease or "",
    )


def _read_limited_response(response, limit: int, *, too_large: str,
                           progress: Callable[[int, int], None] | None = None,
                           output=None) -> tuple[bytes, int]:
    content_length = response.headers.get("Content-Length")
    try:
        total = int(content_length) if content_length else 0
    except (TypeError, ValueError):
        total = 0
    if total > limit:
        raise UpdateCheckError(too_large)
    chunks: list[bytes] = []
    downloaded = 0
    while True:
        chunk = response.read(min(1024 * 1024, limit - downloaded + 1))
        if not chunk:
            break
        downloaded += len(chunk)
        if downloaded > limit:
            raise UpdateCheckError(too_large)
        if output is None:
            chunks.append(chunk)
        else:
            output.write(chunk)
        if progress is not None:
            progress(downloaded, total)
    return b"".join(chunks), downloaded


def _read_manifest(url: str) -> dict:
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "QuizForge-Update-Check/1",
            "X-QuizForge-Platform": platform.system().lower() or "unknown",
        },
        method="GET",
    )
    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            final_url = _https_url(response.geturl() or url, field="更新清单")
            payload, _ = _read_limited_response(
                response, MAX_MANIFEST_BYTES, too_large="更新清单过大"
            )
    except UpdateCheckError:
        raise
    except HTTPError as exc:
        if exc.code == 404:
            raise _NoPublishedUpdate("尚未发布可用更新") from exc
        raise UpdateCheckError("无法读取更新清单") from exc
    except (URLError, TimeoutError, OSError, ValueError) as exc:
        raise UpdateCheckError("无法读取更新清单") from exc

    try:
        raw = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UpdateCheckError("更新清单不是有效 JSON") from exc
    if not isinstance(raw, dict):
        raise UpdateCheckError("更新清单结构无效")
    raw["_manifest_url"] = final_url
    return raw


def check(current_version: str, manifest_url: str) -> dict[str, object]:
    """读取并校验更新清单，返回可直接给前端的非敏感结果。"""
    configured_url = str(manifest_url or "").strip()
    result: dict[str, object] = {
        "enabled": bool(configured_url),
        "current_version": str(current_version or "").strip(),
        "available": False,
    }
    if not configured_url:
        result["message"] = "未配置更新地址"
        return result

    manifest_url = _https_url(configured_url, field="更新清单")
    current_key = _version_key(current_version)
    try:
        raw = _read_manifest(manifest_url)
    except _NoPublishedUpdate:
        result["message"] = "尚未发布可用更新"
        return result
    latest = raw.get("latest_version", raw.get("version"))
    latest_key = _version_key(latest)
    download_url = raw.get("download_url", "")
    if download_url:
        download_url = _https_url(download_url, field="下载")
    sha256 = str(raw.get("sha256", "") or "").strip().lower()
    if sha256 and not _SHA256_RE.fullmatch(sha256):
        raise UpdateCheckError("更新清单 SHA-256 无效")
    signer_thumbprint = re.sub(
        r"\s+", "", str(raw.get("signer_thumbprint", "") or "")
    ).lower()
    if signer_thumbprint and not _THUMBPRINT_RE.fullmatch(signer_thumbprint):
        raise UpdateCheckError("更新清单签名证书指纹无效")
    notes = str(raw.get("notes", "") or "").strip()
    if len(notes) > 4000:
        raise UpdateCheckError("更新说明过长")
    available = latest_key > current_key
    installable = bool(available and download_url and sha256 and signer_thumbprint)
    result.update({
        "latest_version": str(latest).strip(),
        "available": available,
        "installable": installable,
        "download_url": download_url,
        "sha256": sha256,
        # 证书指纹是公开标识，不是凭据；前端仅用于说明，安装时仍重新读清单。
        "signer_thumbprint": signer_thumbprint,
        "notes": notes,
        "published_at": str(raw.get("published_at", "") or "").strip()[:80],
        "manifest_url": raw.get("_manifest_url", manifest_url),
        "message": "发现新版本" if available else "当前已是最新版本",
    })
    return result


def _download_installer(url: str, destination: Path, expected_sha256: str,
                        progress: Callable[[int, int], None] | None = None) -> Path:
    url = _https_url(url, field="下载")
    expected = str(expected_sha256 or "").strip().lower()
    if not _SHA256_RE.fullmatch(expected):
        raise UpdateCheckError("更新清单缺少有效 SHA-256")
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".part")
    request = Request(
        url,
        headers={
            "Accept": "application/octet-stream",
            "User-Agent": "QuizForge-Updater/1",
        },
        method="GET",
    )
    try:
        with urlopen(request, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
            _https_url(response.geturl() or url, field="下载")
            with temporary.open("wb") as output:
                _, downloaded = _read_limited_response(
                    response, MAX_INSTALLER_BYTES,
                    too_large="安装包超过 512 MiB 安全上限",
                    progress=progress, output=output,
                )
        if downloaded == 0:
            raise UpdateCheckError("下载的安装包为空")
        digest = hashlib.sha256()
        with temporary.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        actual = digest.hexdigest()
        if actual != expected:
            raise UpdateCheckError("安装包 SHA-256 校验失败")
        temporary.replace(destination)
        return destination
    except UpdateCheckError:
        raise
    except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
        raise UpdateCheckError("下载安装包失败") from exc
    finally:
        temporary.unlink(missing_ok=True)


def _authenticode_signature(path: Path) -> dict[str, str]:
    if os.name != "nt" or not Path(_POWERSHELL).is_file():
        raise UpdateCheckError("当前系统无法校验 Authenticode 签名")
    env = os.environ.copy()
    env["QUIZFORGE_UPDATE_VERIFY_PATH"] = str(Path(path).resolve())
    script = (
        "$p=[Environment]::GetEnvironmentVariable('QUIZFORGE_UPDATE_VERIFY_PATH');"
        "$s=Get-AuthenticodeSignature -LiteralPath $p;"
        "$t=if($s.SignerCertificate){$s.SignerCertificate.Thumbprint}else{''};"
        "@{Status=[string]$s.Status;Thumbprint=[string]$t;"
        "Subject=if($s.SignerCertificate){[string]$s.SignerCertificate.Subject}else{''}}"
        "|ConvertTo-Json -Compress"
    )
    completed = subprocess.run(
        [_POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=30, env=env, check=False,
    )
    if completed.returncode != 0:
        raise UpdateCheckError("无法读取安装包 Authenticode 签名")
    try:
        value = json.loads(completed.stdout.strip())
    except json.JSONDecodeError as exc:
        raise UpdateCheckError("安装包签名校验返回异常") from exc
    if not isinstance(value, dict):
        raise UpdateCheckError("安装包签名校验返回异常")
    return {str(key): str(item or "") for key, item in value.items()}


def verify_authenticode(path: Path, expected_thumbprint: str) -> dict[str, str]:
    expected = re.sub(r"\s+", "", str(expected_thumbprint or "")).lower()
    if not _THUMBPRINT_RE.fullmatch(expected):
        raise UpdateCheckError("更新清单缺少有效签名证书指纹")
    signature = _authenticode_signature(Path(path))
    actual = re.sub(r"\s+", "", signature.get("Thumbprint", "")).lower()
    if signature.get("Status") != "Valid":
        raise UpdateCheckError("安装包 Authenticode 签名无效或不受信任")
    if actual != expected:
        raise UpdateCheckError("安装包签名证书与更新清单不一致")
    return signature


def prepare_update(current_version: str, manifest_url: str, update_dir: Path,
                   progress: Callable[[int, int], None] | None = None) -> dict[str, object]:
    """重新读取清单、下载安装包并完成全部安全校验。"""
    manifest = check(current_version, manifest_url)
    if not manifest.get("available"):
        raise UpdateCheckError("当前已是最新版本")
    if not manifest.get("installable"):
        raise UpdateCheckError("更新清单缺少安装包哈希或签名信息")
    safe_version = str(manifest["latest_version"]).replace("-", "_")
    destination = Path(update_dir) / f"QuizForge-{safe_version}-Setup.exe"
    _download_installer(
        str(manifest["download_url"]), destination, str(manifest["sha256"]), progress
    )
    signature = verify_authenticode(destination, str(manifest["signer_thumbprint"]))
    return {**manifest, "installer_path": str(destination), "signer": signature}


_LAUNCHER_SCRIPT = r"""param(
    [Parameter(Mandatory=$true)][int]$ParentPid,
    [Parameter(Mandatory=$true)][string]$Installer,
    [Parameter(Mandatory=$true)][string]$InstallDir,
    [Parameter(Mandatory=$true)][string]$AppExe,
    [Parameter(Mandatory=$true)][string]$StatusPath,
    [Parameter(Mandatory=$true)][string]$LogPath
)
$ErrorActionPreference = 'Stop'
function Write-Status([string]$State, [string]$Message, [int]$ExitCode = 0) {
    $temp = "$StatusPath.tmp"
    @{status=$State; message=$Message; exit_code=$ExitCode; updated_at=(Get-Date).ToUniversalTime().ToString('o')} |
        ConvertTo-Json -Compress | Set-Content -LiteralPath $temp -Encoding UTF8
    Move-Item -LiteralPath $temp -Destination $StatusPath -Force
}
try {
    Write-Status 'waiting' '正在关闭 QuizForge'
    $deadline = (Get-Date).AddSeconds(90)
    while ((Get-Process -Id $ParentPid -ErrorAction SilentlyContinue) -and (Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 250
    }
    if (Get-Process -Id $ParentPid -ErrorAction SilentlyContinue) {
        throw 'QuizForge 未能在 90 秒内退出'
    }
    Write-Status 'installing' '正在覆盖安装'
    $arguments = @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/CURRENTUSER',
        '/CLOSEAPPLICATIONS', ('/DIR="{0}"' -f $InstallDir), ('/LOG="{0}"' -f $LogPath))
    $process = Start-Process -FilePath $Installer -ArgumentList $arguments -Wait -PassThru
    if ($process.ExitCode -ne 0) { throw "安装程序退出码 $($process.ExitCode)" }
    if (-not (Test-Path -LiteralPath $AppExe -PathType Leaf)) { throw '更新后未找到 QuizForge.exe' }
    Write-Status 'completed' '更新完成，正在重新启动'
    Start-Process -FilePath $AppExe -WorkingDirectory $InstallDir
} catch {
    Write-Status 'failed' ([string]$_.Exception.Message) 1
}
"""


def launch_installer(installer: Path, install_dir: Path, app_exe: Path,
                     update_dir: Path, *, parent_pid: int | None = None) -> Path:
    """启动独立覆盖进程；调用方随后应关闭桌面窗口并退出主进程。"""
    if os.name != "nt" or not Path(_POWERSHELL).is_file():
        raise UpdateCheckError("当前系统不支持一键覆盖安装")
    installer = Path(installer).resolve()
    install_dir = Path(install_dir).resolve()
    app_exe = Path(app_exe).resolve()
    if not installer.is_file() or installer.suffix.lower() != ".exe":
        raise UpdateCheckError("待安装文件不存在")
    if not app_exe.is_file() or app_exe.parent != install_dir:
        raise UpdateCheckError("当前安装目录无效")
    update_dir = Path(update_dir).resolve()
    update_dir.mkdir(parents=True, exist_ok=True)
    launcher = update_dir / "apply-update.ps1"
    launcher.write_text(_LAUNCHER_SCRIPT, encoding="utf-8-sig")
    status_path = update_dir / "update-status.json"
    log_path = update_dir / "installer.log"
    command = [
        _POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
        "-File", str(launcher),
        "-ParentPid", str(parent_pid or os.getpid()),
        "-Installer", str(installer),
        "-InstallDir", str(install_dir),
        "-AppExe", str(app_exe),
        "-StatusPath", str(status_path),
        "-LogPath", str(log_path),
    ]
    creationflags = (
        getattr(subprocess, "CREATE_NO_WINDOW", 0)
        | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    )
    try:
        subprocess.Popen(
            command, cwd=str(update_dir), close_fds=True,
            creationflags=creationflags,
        )
    except OSError as exc:
        raise UpdateCheckError("无法启动独立更新程序") from exc
    return status_path


def previous_update_status(update_dir: Path) -> dict[str, object]:
    path = Path(update_dir) / "update-status.json"
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}
