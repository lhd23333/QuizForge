<#
.SYNOPSIS
    安装 / 卸载 QuizForge 独立 agent（`qf`）。

.DESCRIPTION
    把 integrations\qf-agent\agent\ 整份安装到**独立目录**：

        %LOCALAPPDATA%\QuizForge\agent      （可用 -AgentDir 覆盖）

    `qf.cmd` 启动 pi 时设置 PI_CODING_AGENT_DIR 指向该目录，因此它与用户的
    通用 pi 配置（~/.pi/agent）**完全隔离**：不共用 mcp.json、扩展、skill、
    提示词、会话历史，也不共用模型配置（models.json / auth.json）。

    安装时会：
      1. 复制 agent 资源（settings / SYSTEM.md / 扩展 / skill / 专属命令）；
      2. 生成 mcp.json —— 写入本机 .venv 的 python 绝对路径，以及从桌面版
         desktop.json 取到的题库根 / 科目 / 共享图片目录（自包含，不依赖环境变量）；
      3. 首次安装默认从 ~/.pi/agent 拷贝 models.json 与 auth.json 作为**初始**
         模型配置（之后两者独立演进；用 -SkipImport 跳过，改为在 qf 里 /login）；
      4. 清理之前在 ~/.pi/agent 里装过的 QuizForge 资源（避免两套并存）。

.EXAMPLE
    pwsh -File integrations\qf-agent\install.ps1
    pwsh -File integrations\qf-agent\install.ps1 -SkipImport
    pwsh -File integrations\qf-agent\install.ps1 -AgentDir "D:\qf-agent"
    pwsh -File integrations\qf-agent\install.ps1 -Uninstall
#>
[CmdletBinding()]
param(
    # 独立 agent 目录；默认 %LOCALAPPDATA%\QuizForge\agent
    [string]$AgentDir = "",
    # 不导入 ~/.pi/agent 的模型配置（改为在 qf 里 /login 或自己写 models.json）
    [switch]$SkipImport,
    # 不清理 ~/.pi/agent 里的 QuizForge 残留
    [switch]$KeepGlobal,
    # 题库根（可选）：不传则从桌面版 desktop.json 取第一个登记的题库
    [string]$Bank = "",
    # 卸载：删除独立 agent 目录（含会话历史）
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"

$PkgDir = $PSScriptRoot                        # integrations\qf-agent
$RepoRoot = Split-Path -Parent (Split-Path -Parent $PkgDir)
$TemplateDir = Join-Path $PkgDir "agent"
$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$ServerScript = Join-Path $RepoRoot "tools\qf_mcp_server.py"

$PiAgentDir = Join-Path $env:USERPROFILE ".pi\agent"
$DesktopJson = Join-Path $env:LOCALAPPDATA "QuizForge\desktop.json"

if (-not $AgentDir) { $AgentDir = Join-Path $env:LOCALAPPDATA "QuizForge\agent" }
$AgentDir = $AgentDir.TrimEnd('\')

function Write-Json([string]$Path, [string]$Text) {
    # pi 用 Node 读 JSON，JSON.parse 不接受 BOM。
    [IO.File]::WriteAllText($Path, $Text, [Text.UTF8Encoding]::new($false))
}

# --------------------------------------------------------------------------- 卸载
if ($Uninstall) {
    if (Test-Path $AgentDir) {
        $sessions = Join-Path $AgentDir "sessions"
        if (Test-Path $sessions) {
            Write-Warning "该目录含 qf 的会话历史，将一并删除：$sessions"
        }
        Remove-Item $AgentDir -Recurse -Force
        Write-Host "[qf-agent] 已删除独立 agent 目录：$AgentDir" -ForegroundColor Green
    } else {
        Write-Host "[qf-agent] 未安装（找不到 $AgentDir）" -ForegroundColor Yellow
    }
    Write-Host "[qf-agent] 提示：通用 pi 的配置（~/.pi/agent）未受影响。"
    exit 0
}

if (-not (Test-Path $TemplateDir)) { throw "找不到 agent 模板目录：$TemplateDir" }
if (-not (Test-Path $Python)) { throw "找不到 QuizForge 的虚拟环境：$Python" }
if (-not (Test-Path $ServerScript)) { throw "找不到 MCP server：$ServerScript" }

# ----------------------------------------------------------- 从桌面版取题库信息
$bankPath = $Bank
$bankSubject = "math"
$assetsDir = ""
if (Test-Path $DesktopJson) {
    try {
        $desktop = Get-Content $DesktopJson -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($desktop.PSObject.Properties["assets_dir"] -and $desktop.assets_dir) {
            $assetsDir = [string]$desktop.assets_dir
        }
        if (-not $bankPath -and $desktop.PSObject.Properties["bank_dir"] -and $desktop.bank_dir) {
            $bankPath = [string]$desktop.bank_dir
        }
        if ($bankPath -and $desktop.PSObject.Properties["banks"] -and $desktop.banks) {
            foreach ($bank in $desktop.banks) {
                if (([string]$bank.path).TrimEnd('\') -eq $bankPath.TrimEnd('\')) {
                    if ($bank.PSObject.Properties["subject"] -and $bank.subject) {
                        $bankSubject = [string]$bank.subject
                    }
                    break
                }
            }
        }
    } catch {
        Write-Warning "desktop.json 读取失败，题库需稍后用 qf-bank 指定：$($_.Exception.Message)"
    }
}
if ($bankPath -and -not (Test-Path $bankPath)) {
    Write-Warning "题库目录不存在，稍后用 qf-bank 重新指定：$bankPath"
}

# ---------------------------------------------------------------------------- 安装
New-Item -ItemType Directory -Force -Path $AgentDir | Out-Null
# settings.json 不随模板覆盖：已安装过时只补缺失的键，保住用户的改动与 pi 自己写的状态。
Get-ChildItem $TemplateDir -Force | Where-Object { $_.Name -ne "settings.json" } | ForEach-Object {
    Copy-Item $_.FullName $AgentDir -Recurse -Force
}
$settingsPath = Join-Path $AgentDir "settings.json"
$templateSettings = Get-Content (Join-Path $TemplateDir "settings.json") -Raw -Encoding UTF8 | ConvertFrom-Json
if (Test-Path $settingsPath) {
    $currentSettings = Get-Content $settingsPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $added = @()
    foreach ($prop in $templateSettings.PSObject.Properties) {
        if (-not $currentSettings.PSObject.Properties[$prop.Name]) {
            $currentSettings | Add-Member -NotePropertyName $prop.Name -NotePropertyValue $prop.Value
            $added += $prop.Name
        }
    }
    if ($added.Count) {
        Write-Json $settingsPath ($currentSettings | ConvertTo-Json -Depth 20)
        Write-Host "[qf-agent] settings.json 已补齐：$($added -join ', ')" -ForegroundColor Green
    }
} else {
    Write-Json $settingsPath ($templateSettings | ConvertTo-Json -Depth 20)
}
Write-Host "[qf-agent] 已同步 agent 资源到 $AgentDir" -ForegroundColor Green

# mcp.json：自包含，写绝对路径 + 题库信息
$serverEnv = [ordered]@{ QF_MODE = "danger" }
if ($bankPath) { $serverEnv["QUIZFORGE_BANK"] = $bankPath }
if ($bankSubject) { $serverEnv["QUIZFORGE_SUBJECT"] = $bankSubject }
if ($assetsDir) { $serverEnv["QUIZFORGE_ASSETS_DIR"] = $assetsDir }
$mcp = [ordered]@{
    mcpServers = [ordered]@{
        quizforge = [ordered]@{
            command  = $Python
            args     = @($ServerScript)
            env      = [pscustomobject]$serverEnv
            exposure = "codemode"
        }
    }
}
Write-Json (Join-Path $AgentDir "mcp.json") ($mcp | ConvertTo-Json -Depth 20)
Write-Host "[qf-agent] 已生成 mcp.json（server 指向本地 venv，题库=$bankPath）" -ForegroundColor Green

# 初始模型配置（一次性拷贝，之后各自独立）
if (-not $SkipImport) {
    foreach ($name in @("models.json", "auth.json")) {
        $source = Join-Path $PiAgentDir $name
        $dest = Join-Path $AgentDir $name
        if ((Test-Path $source) -and -not (Test-Path $dest)) {
            Copy-Item $source $dest -Force
            Write-Host "[qf-agent] 已导入初始 $name（来自通用 pi 配置；之后两者独立）" -ForegroundColor Green
        }
    }
}

# 清理 ~/.pi/agent 里的 QuizForge 残留，避免两套并存
if (-not $KeepGlobal) {
    $removed = @()
    foreach ($name in @("qf-brand.ts", "qf-permissions.ts")) {
        $p = Join-Path $PiAgentDir "extensions\$name"
        if (Test-Path $p) { Remove-Item $p -Force; $removed += "extensions\$name" }
    }
    $skill = Join-Path $PiAgentDir "skills\quizforge"
    if (Test-Path $skill) { Remove-Item $skill -Recurse -Force; $removed += "skills\quizforge" }
    Get-ChildItem (Join-Path $PiAgentDir "prompts") -Filter "qf-*.md" -ErrorAction SilentlyContinue | ForEach-Object {
        Remove-Item $_.FullName -Force; $removed += "prompts\$($_.Name)"
    }
    $mcpPath = Join-Path $PiAgentDir "mcp.json"
    if (Test-Path $mcpPath) {
        try {
            $globalMcp = Get-Content $mcpPath -Raw -Encoding UTF8 | ConvertFrom-Json
            if ($globalMcp.mcpServers.PSObject.Properties["quizforge"]) {
                $globalMcp.mcpServers.PSObject.Properties.Remove("quizforge")
                Write-Json $mcpPath ($globalMcp | ConvertTo-Json -Depth 20)
                $removed += "mcp.json:quizforge"
            }
        } catch {
            Write-Warning "清理通用 pi 的 mcp.json 失败：$($_.Exception.Message)"
        }
    }
    if ($removed.Count) {
        Write-Host "[qf-agent] 已从通用 pi 配置移除 QuizForge 资源（$($removed -join ', ')）" -ForegroundColor Green
    }
}

Write-Host ""
Write-Host "[qf-agent] 安装完成。" -ForegroundColor Cyan
Write-Host "  agent 目录 ：$AgentDir"
Write-Host "  默认模型   ：magpie / deepseek/deepseek-flash（本机网关，Ctrl+P 可切换 magpie 的其它模型）"
Write-Host "  题库       ：$(if ($bankPath) { $bankPath } else { '未指定（用 qf-bank 或对话里 /bank 指定）' })"
Write-Host "  科目       ：$bankSubject"
if ($assetsDir) { Write-Host "  共享图片   ：$assetsDir" }
Write-Host ""
Write-Host "  下一步："
Write-Host "    qf                 进入 QuizForge agent"
Write-Host "    qf-bank            查看/切换题库"
Write-Host "  说明：本目录与通用 pi（~/.pi/agent）完全隔离，互不影响。"
