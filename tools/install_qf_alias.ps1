<#
.SYNOPSIS
    安装 / 卸载全局 qf 命令组（QuizForge 专属 agent / 自研 TUI / MCP server）。

.DESCRIPTION
    做两件事：
      1. 把源码目录写进用户级环境变量 QF_ROOT（注册表存储，Unicode 安全）；
      2. 在「已位于用户 PATH」的目录里放三个纯 ASCII 转发脚本：

           qf       pi 上的 QuizForge 专属 agent（自动加载 软件版\.pi\ 项目配置）
           qf-tui   QuizForge 自研终端界面（零外部依赖，离线可用）
           qf-mcp   QuizForge MCP server（stdio，供 pi 等 MCP 客户端）

    转发脚本刻意不含中文：cmd 按控制台代码页读批处理文件，写死中文路径会随终端
    代码页（936 / 65001）而失效；QF_ROOT 走 Unicode 环境变量则不受影响。

    本脚本不改系统 PATH、不写系统级变量、不动安装目录；-Uninstall 完全还原。
    源码目录移动后重跑本脚本即可修复。

.EXAMPLE
    pwsh -File tools\install_qf_alias.ps1
    pwsh -File tools\install_qf_alias.ps1 -TargetDir "$env:USERPROFILE\.local\bin"
    pwsh -File tools\install_qf_alias.ps1 -Uninstall
#>
[CmdletBinding()]
param(
    # 启动器安装目录；默认自动挑选一个已存在于用户 PATH 且存在的目录
    [string]$TargetDir = "",
    # 题库根目录（可选）：写入用户级环境变量 QUIZFORGE_BANK，QuizForge 与 pi 都会用它
    [string]$Bank = "",
    # 题库内工作目录（可选）：写入用户级环境变量 QF_WORKDIR
    [string]$Workdir = "",
    # 卸载：删除启动器，并在 QF_ROOT 指向本仓库时清掉该变量
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"
$EnvVarName = "QF_ROOT"
# 每个条目：命令名 -> 仓库内的目标脚本（必须存在）
$Commands = [ordered]@{
    "qf"      = "qf.cmd"
    "qf-tui"  = "qf-tui.cmd"
    "qf-mcp"  = "qf-mcp.cmd"
    "qf-bank" = "qf-bank.cmd"
}

$RepoRoot = Split-Path -Parent $PSScriptRoot
if (-not (Test-Path (Join-Path $RepoRoot "qf.cmd"))) {
    throw "找不到 QuizForge 源码（缺少 qf.cmd）：$RepoRoot"
}
foreach ($target in $Commands.Values) {
    if (-not (Test-Path (Join-Path $RepoRoot $target))) {
        throw "仓库里缺少启动器 $target；请更新到包含它版本。"
    }
}

function Get-UserPathEntries {
    $raw = [Environment]::GetEnvironmentVariable("PATH", "User")
    if (-not $raw) { return @() }
    return @($raw -split ';' | Where-Object { $_ } | ForEach-Object { $_.TrimEnd('\') })
}

function Resolve-TargetDir {
    if ($TargetDir) {
        return (New-Item -ItemType Directory -Force -Path $TargetDir).FullName
    }
    $userPath = Get-UserPathEntries
    $candidates = @(
        (Join-Path $env:USERPROFILE ".local\bin"),
        (Join-Path $env:LOCALAPPDATA "Microsoft\WindowsApps")
    )
    foreach ($candidate in $candidates) {
        if (-not (Test-Path $candidate)) { continue }
        if ($userPath -notcontains $candidate.TrimEnd('\')) { continue }
        return $candidate
    }
    $fallback = Join-Path $env:USERPROFILE "bin"
    New-Item -ItemType Directory -Force -Path $fallback | Out-Null
    Write-Warning "用户 PATH 里没有合适的候选目录，已改用 $fallback。"
    Write-Warning "请执行下面这条命令把它加入用户 PATH，然后重开终端："
    Write-Warning "  [Environment]::SetEnvironmentVariable('PATH', [Environment]::GetEnvironmentVariable('PATH','User') + ';$fallback', 'User')"
    return $fallback
}

# pi 用 Node 读 mcp.json，JSON.parse 不接受 BOM，所以统一写无 BOM 的 UTF-8。
function Write-Ascii([string]$Path, [string]$Text) {
    [IO.File]::WriteAllText($Path, $Text, [Text.UTF8Encoding]::new($false))
}

$target = (Resolve-TargetDir).TrimEnd('\')
$previousRoot = [Environment]::GetEnvironmentVariable($EnvVarName, "User")

if ($Uninstall) {
    $removed = 0
    foreach ($name in $Commands.Keys) {
        $path = Join-Path $target "$name.cmd"
        if (Test-Path $path) { Remove-Item $path -Force; $removed++ }
    }
    Write-Host "[qf] 已移除 $removed 个启动器（$($Commands.Keys -join ', ')）" -ForegroundColor Green
    if ($previousRoot -and ($previousRoot.TrimEnd('\') -eq $RepoRoot.TrimEnd('\'))) {
        [Environment]::SetEnvironmentVariable($EnvVarName, $null, "User")
        Write-Host "[qf] 已清除用户环境变量 $EnvVarName" -ForegroundColor Green
    } elseif ($previousRoot) {
        Write-Host "[qf] 保留现有 $EnvVarName（指向 $previousRoot，非本仓库）" -ForegroundColor Yellow
    }
    Write-Host "[qf] 提示：若曾用 -Bank / -Workdir 设置过 QUIZFORGE_BANK / QF_WORKDIR，请自行清除：" -ForegroundColor DarkGray
    Write-Host "      [Environment]::SetEnvironmentVariable('QUIZFORGE_BANK', `$null, 'User')" -ForegroundColor DarkGray
    exit 0
}

[Environment]::SetEnvironmentVariable($EnvVarName, $RepoRoot, "User")

# 题库根 / 工作目录走用户级环境变量：注册表存储是 Unicode 的，不受终端代码页影响，
# 同时 GUI 之外的入口（pi 里的 MCP server、qf-tui）都会自动用上同一个题库。
$optional = @(
    [pscustomobject]@{ Name = "QUIZFORGE_BANK"; Value = $Bank; MustExist = $true },
    [pscustomobject]@{ Name = "QF_WORKDIR"; Value = $Workdir; MustExist = $false }
)
foreach ($item in $optional) {
    if (-not $item.Value) { continue }
    if ($item.MustExist -and -not (Test-Path $item.Value)) {
        throw "$($item.Name) 指向的目录不存在：$($item.Value)"
    }
    [Environment]::SetEnvironmentVariable($item.Name, $item.Value, "User")
    Write-Host "[qf] 已设置用户环境变量 $($item.Name)=$($item.Value)" -ForegroundColor Green
}

foreach ($name in $Commands.Keys) {
    $targetScript = $Commands[$name]
    $content = @"
@echo off
rem QuizForge launcher (global). Installed by tools/install_qf_alias.ps1
rem Forwards to the repo launcher $targetScript using %QF_ROOT%.
if not defined $EnvVarName (
  echo [$name] $EnvVarName is not set. Open a NEW terminal, or re-run tools\install_qf_alias.ps1.
  exit /b 1
)
if not exist "%$EnvVarName%\$targetScript" (
  echo [$name] QuizForge source not found: %$EnvVarName%
  echo [$name] Re-run tools\install_qf_alias.ps1 inside the QuizForge repo.
  exit /b 1
)
call "%$EnvVarName%\$targetScript" %*
exit /b %ERRORLEVEL%
"@
    $content = ($content -replace "`r`n", "`n") -replace "`n", "`r`n"
    Write-Ascii (Join-Path $target "$name.cmd") $content
    Write-Host "[qf] 已安装 $name.cmd -> $targetScript" -ForegroundColor Green
}

Write-Host ""
Write-Host "[qf] 源码目录：$RepoRoot"
Write-Host "[qf] 用户环境变量：$EnvVarName=$RepoRoot"
if ($previousRoot -ne $RepoRoot) {
    Write-Host "[qf] 注意：请新开一个终端再使用（当前终端看不到刚写入的环境变量）。" -ForegroundColor Yellow
}
Write-Host "[qf] 用法："
Write-Host "       qf            QuizForge 专属 agent（基于 pi，带题库工具与权限门）"
Write-Host "       qf-tui        QuizForge 自研终端界面（零外部依赖）"
Write-Host "       qf-mcp        QuizForge MCP server（stdio）"
Write-Host "[qf] 卸载：pwsh -File tools\install_qf_alias.ps1 -Uninstall"
