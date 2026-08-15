param(
    [string]$Version = "0.17.0-beta",
    [string]$FileVersion = "0.17.0.0",
    [switch]$SkipDesktopBuild
)

# Keep this Windows PowerShell 5.1 script ASCII-only; see build_desktop.ps1.
$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$desktopDist = Join-Path $projectRoot "build\desktop\QuizForge"
$pandoc = Join-Path $projectRoot "runtime\pandoc\pandoc.exe"
$pandocLicense = Join-Path $projectRoot "runtime\pandoc\licenses\COPYING.md"
$pandocSource = Join-Path $projectRoot "runtime\pandoc\source\pandoc-3.9.0.2.tar.gz"

foreach ($required in @($pandoc, $pandocLicense, $pandocSource)) {
    if (-not (Test-Path -LiteralPath $required)) {
        throw "Required release component missing: $required"
    }
}

if (-not $SkipDesktopBuild) {
    & (Join-Path $projectRoot "build_desktop.ps1")
    if ($LASTEXITCODE -ne 0) { throw "Desktop build failed" }
}
if (-not (Test-Path -LiteralPath (Join-Path $desktopDist "QuizForge.exe"))) {
    throw "Desktop distribution not found: $desktopDist"
}

$isccCandidates = @(@(
    (Get-Command "ISCC.exe" -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -First 1),
    (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe"),
    (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe")
) | Where-Object { $_ -and (Test-Path -LiteralPath $_) })
if ($isccCandidates.Count -eq 0) {
    throw "Inno Setup 6 compiler not found. Install it before building the installer."
}

$outputDir = Join-Path $projectRoot "build\installer"
New-Item -ItemType Directory -Force -Path $outputDir | Out-Null
$iss = Join-Path $projectRoot "installer\QuizForge.iss"
# Keep compiler output below automation limits. A verbose build lists more than
# 1300 files and can be terminated before the setup executable is finalized.
& $isccCandidates[0] "/Q" "/DMyAppVersion=$Version" "/DMyFileVersion=$FileVersion" `
    "/DMySourceDir=$desktopDist" "/DMyOutputDir=$outputDir" $iss
if ($LASTEXITCODE -ne 0) { throw "Inno Setup build failed with exit code $LASTEXITCODE" }

$installer = Join-Path $outputDir "QuizForge-$Version-Setup.exe"
if (-not (Test-Path -LiteralPath $installer)) {
    throw "Installer not found after build: $installer"
}
Write-Output "[OK] Installer: $installer"
