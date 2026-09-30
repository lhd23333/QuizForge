<#
.SYNOPSIS
    切换 QuizForge agent（qf）使用的题库。

.DESCRIPTION
    题库信息写在**独立 agent 目录**的 mcp.json 里（自包含，不依赖环境变量）：

        %LOCALAPPDATA%\QuizForge\agent\mcp.json
          mcpServers.quizforge.env.QUIZFORGE_BANK / QUIZFORGE_SUBJECT / QUIZFORGE_ASSETS_DIR

    题库清单来自桌面版登记（%LOCALAPPDATA%\QuizForge\desktop.json），
    也可以直接给路径。切完（默认）直接进入 qf。

.EXAMPLE
    qf-bank                 # 列出可选题库与当前值
    qf-bank 物理            # 切到"物理"并进入 qf
    qf-bank 数学 -NoStart   # 只切换，下次 qf 生效
    qf-bank D:\some\vault   # 直接用路径
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)][string]$Target = "",
    [switch]$List,
    [switch]$NoStart
)

$ErrorActionPreference = "Stop"

$AgentDir = $env:QUIZFORGE_AGENT_DIR
if (-not $AgentDir) { $AgentDir = Join-Path $env:LOCALAPPDATA "QuizForge\agent" }
$AgentDir = $AgentDir.TrimEnd('\')
$McpJson = Join-Path $AgentDir "mcp.json"
$DesktopJson = Join-Path $env:LOCALAPPDATA "QuizForge\desktop.json"

function Write-Json([string]$Path, [string]$Text) {
    [IO.File]::WriteAllText($Path, $Text, [Text.UTF8Encoding]::new($false))
}

if (-not (Test-Path $McpJson)) {
    Write-Host "找不到 QuizForge agent 配置：$McpJson" -ForegroundColor Red
    Write-Host "请先运行： pwsh -File integrations\qf-agent\install.ps1" -ForegroundColor Yellow
    exit 1
}

# ----------------------------------------------------------------- 读题库清单
$banks = @()
$assetsDir = ""
if (Test-Path $DesktopJson) {
    try {
        $desktop = Get-Content $DesktopJson -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($desktop.PSObject.Properties["banks"] -and $desktop.banks) { $banks = @($desktop.banks) }
        if ($desktop.PSObject.Properties["assets_dir"] -and $desktop.assets_dir) { $assetsDir = [string]$desktop.assets_dir }
        if ($banks.Count -eq 0 -and $desktop.PSObject.Properties["bank_dir"] -and $desktop.bank_dir) {
            $banks = @([pscustomobject]@{ name = (Split-Path -Leaf ([string]$desktop.bank_dir)); path = [string]$desktop.bank_dir; subject = "math" })
        }
    } catch {
        Write-Warning "desktop.json 读取失败，只支持按路径指定：$($_.Exception.Message)"
    }
}

# ------------------------------------------------------------------- 读当前值
$mcp = Get-Content $McpJson -Raw -Encoding UTF8 | ConvertFrom-Json
$server = $mcp.mcpServers.quizforge
$currentBank = ""
$currentSubject = "math"
if ($server -and $server.PSObject.Properties["env"] -and $server.env) {
    if ($server.env.PSObject.Properties["QUIZFORGE_BANK"]) { $currentBank = [string]$server.env.QUIZFORGE_BANK }
    if ($server.env.PSObject.Properties["QUIZFORGE_SUBJECT"]) { $currentSubject = [string]$server.env.QUIZFORGE_SUBJECT }
}

function Get-SubjectLabel([string]$subject) {
    if ($subject -eq "physics") { return "物理" }
    return "数学"
}

function Show-Banks {
    Write-Host "可选题库（桌面版登记的）：" -ForegroundColor Cyan
    if ($banks.Count -eq 0) {
        Write-Host "  （没有登记记录；可直接用 qf-bank <路径>）"
    } else {
        $index = 1
        foreach ($bank in $banks) {
            $mark = " "
            if ($currentBank -and ($currentBank.TrimEnd('\') -eq ([string]$bank.path).TrimEnd('\'))) { $mark = "*" }
            Write-Host ("  {0} {1}. {2,-10} [{3}] {4}" -f `
                $mark, $index, [string]$bank.name, (Get-SubjectLabel ([string]$bank.subject)), [string]$bank.path)
            $index++
        }
    }
    Write-Host ""
    if ([string]::IsNullOrWhiteSpace($currentBank)) {
        Write-Host "  当前：未指定（QuizForge 用默认题库）"
    } else {
        Write-Host "  当前：$currentBank [$((Get-SubjectLabel $currentSubject))]"
    }
    if ($assetsDir) { Write-Host "  共享图片目录：$assetsDir" }
    Write-Host ""
    Write-Host "用法：qf-bank <名称|路径>   （-NoStart 只切换不启动）" -ForegroundColor DarkGray
}

if ($List -or [string]::IsNullOrWhiteSpace($Target)) {
    Show-Banks
    exit 0
}

# --------------------------------------------------------------------- 选择
$chosen = $null
foreach ($bank in $banks) {
    if (([string]$bank.name) -eq $Target -or ([string]$bank.path) -eq $Target) { $chosen = $bank; break }
}
if (-not $chosen) {
    foreach ($bank in $banks) {
        if (([string]$bank.name) -like "*$Target*" -or ([string]$bank.path) -like "*$Target*") { $chosen = $bank; break }
    }
}
if (-not $chosen -and (Test-Path $Target)) {
    $resolved = (Resolve-Path $Target).Path
    $chosen = [pscustomobject]@{ name = (Split-Path -Leaf $resolved); path = $resolved; subject = "math" }
}
if (-not $chosen) {
    Write-Host "找不到题库：$Target" -ForegroundColor Red
    Write-Host ""
    Show-Banks
    exit 1
}
if (-not (Test-Path ([string]$chosen.path))) {
    Write-Host "题库目录不存在：$($chosen.path)" -ForegroundColor Red
    exit 1
}

$subject = "math"
if ($chosen.PSObject.Properties["subject"] -and $chosen.subject) { $subject = [string]$chosen.subject }

# --------------------------------------------------------------------- 写回
$envObj = [ordered]@{ QF_MODE = "danger" }
if ($server -and $server.PSObject.Properties["env"] -and $server.env) {
    foreach ($prop in $server.env.PSObject.Properties) { $envObj[$prop.Name] = $prop.Value }
}
$envObj["QUIZFORGE_BANK"] = [string]$chosen.path
$envObj["QUIZFORGE_SUBJECT"] = $subject
if ($assetsDir) { $envObj["QUIZFORGE_ASSETS_DIR"] = $assetsDir }

$server.env = [pscustomobject]$envObj
Write-Json $McpJson ($mcp | ConvertTo-Json -Depth 20)

Write-Host "[qf] 已切换题库：$($chosen.name)" -ForegroundColor Green
Write-Host "     $($chosen.path)"
Write-Host "     科目 $(Get-SubjectLabel $subject)（QUIZFORGE_SUBJECT=$subject）"
if ($assetsDir) { Write-Host "     共享图片 $assetsDir" }
Write-Host "     写入 $McpJson"

if ($NoStart) {
    Write-Host "[qf] 下次运行 qf 生效。" -ForegroundColor DarkGray
    exit 0
}

$launcher = Join-Path (Split-Path -Parent $PSScriptRoot) "qf.cmd"
if (-not (Test-Path $launcher)) {
    Write-Host "[qf] 找不到启动器 $launcher" -ForegroundColor Red
    exit 1
}
& $launcher
exit $LASTEXITCODE
