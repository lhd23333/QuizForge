param(
    [string]$Version = "1.0.0",
    [string]$FileVersion = "1.0.0.0",
    [switch]$SkipDesktopBuild,
    [string]$SigningCertificateThumbprint = $env:QUIZFORGE_SIGNING_CERT_THUMBPRINT,
    [string]$TimestampUrl = "http://timestamp.digicert.com",
    [switch]$AllowUnsigned
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

$SigningCertificateThumbprint = ($SigningCertificateThumbprint -replace '\s', '').ToUpperInvariant()
$formalRelease = $Version -match '^\d+\.\d+\.\d+$'
if ($formalRelease -and -not $SigningCertificateThumbprint -and -not $AllowUnsigned) {
    throw "Formal releases require QUIZFORGE_SIGNING_CERT_THUMBPRINT"
}
if ($SigningCertificateThumbprint -and $SigningCertificateThumbprint -notmatch '^[0-9A-F]{40}$') {
    throw "Signing certificate thumbprint must be 40 hexadecimal characters"
}

$signTool = $null
if ($SigningCertificateThumbprint) {
    $signTool = Get-Command "signtool.exe" -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty Source -First 1
    if (-not $signTool) {
        $kitsRoot = Join-Path ${env:ProgramFiles(x86)} "Windows Kits\10\bin"
        $signTool = Get-ChildItem -LiteralPath $kitsRoot -Filter "signtool.exe" -File -Recurse `
            -ErrorAction SilentlyContinue | Where-Object { $_.FullName -like '*\x64\signtool.exe' } |
            Sort-Object FullName -Descending | Select-Object -ExpandProperty FullName -First 1
    }
    if (-not $signTool) { throw "signtool.exe was not found" }
}

function Sign-Artifact([string]$Path) {
    if (-not $SigningCertificateThumbprint) { return }
    & $signTool sign /sha1 $SigningCertificateThumbprint /fd SHA256 /tr $TimestampUrl /td SHA256 $Path
    if ($LASTEXITCODE -ne 0) { throw "Signing failed: $Path" }
    & $signTool verify /pa /q $Path
    if ($LASTEXITCODE -ne 0) { throw "Signature verification failed: $Path" }
}

if (-not $SkipDesktopBuild) {
    & (Join-Path $projectRoot "build_desktop.ps1") -Version $Version -FileVersion $FileVersion
    if ($LASTEXITCODE -ne 0) { throw "Desktop build failed" }
}
if (-not (Test-Path -LiteralPath (Join-Path $desktopDist "QuizForge.exe"))) {
    throw "Desktop distribution not found: $desktopDist"
}
Sign-Artifact (Join-Path $desktopDist "QuizForge.exe")

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
Sign-Artifact $installer
Write-Output "[OK] Installer: $installer"
