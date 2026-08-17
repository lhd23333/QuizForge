param(
    [string]$RuntimeRoot = ""
)

# Keep this Windows PowerShell 5.1 script ASCII-only.
$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
if (-not $RuntimeRoot) {
    $RuntimeRoot = Join-Path $projectRoot "runtime\pandoc"
}
$RuntimeRoot = [System.IO.Path]::GetFullPath($RuntimeRoot)

$pandocVersion = "3.9.0.2"
$archiveName = "pandoc-3.9.0.2-windows-x86_64.zip"
$downloadRoot = Join-Path $projectRoot "build\ci-runtime-downloads"
$extractRoot = Join-Path $projectRoot "build\ci-runtime-extract"

$resources = @(
    @{
        Name = "COPYING.md"
        Uri = "https://raw.githubusercontent.com/jgm/pandoc/$pandocVersion/COPYING.md"
        Sha256 = "9D56CAC92294E206AF026A5502BEE0FED77200B08B51EC28AA63C9EFDA4DCFDD"
        Destination = Join-Path $RuntimeRoot "licenses\COPYING.md"
    },
    @{
        Name = "COPYRIGHT"
        Uri = "https://raw.githubusercontent.com/jgm/pandoc/$pandocVersion/COPYRIGHT"
        Sha256 = "842E33EF01625E93F85BEBB8BAC83AA570186B7AA77A09971257CC29F8F60740"
        Destination = Join-Path $RuntimeRoot "licenses\COPYRIGHT"
    },
    @{
        Name = "pandoc-$pandocVersion.tar.gz"
        Uri = "https://hackage.haskell.org/package/pandoc-$pandocVersion/pandoc-$pandocVersion.tar.gz"
        Sha256 = "6446A83129485AAD1796E574BF46922A837D0B0537D86F215DB308AB931D2B6C"
        Destination = Join-Path $RuntimeRoot "source\pandoc-$pandocVersion.tar.gz"
    }
)

function Get-Sha256([string]$Path) {
    return (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash.ToUpperInvariant()
}

function Assert-Hash([string]$Path, [string]$Expected) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Required file is missing: $Path"
    }
    $actual = Get-Sha256 $Path
    if ($actual -ne $Expected) {
        throw "SHA-256 mismatch for $Path. Expected $Expected, got $actual"
    }
}

function Get-VerifiedDownload([string]$Uri, [string]$Path, [string]$Sha256) {
    if (Test-Path -LiteralPath $Path -PathType Leaf) {
        Assert-Hash $Path $Sha256
        return
    }
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Path) | Out-Null
    Invoke-WebRequest -UseBasicParsing -Uri $Uri -OutFile $Path
    Assert-Hash $Path $Sha256
}

function Copy-VerifiedFile([string]$Source, [string]$Destination, [string]$Sha256) {
    if (Test-Path -LiteralPath $Destination -PathType Leaf) {
        Assert-Hash $Destination $Sha256
        return
    }
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Destination) | Out-Null
    Copy-Item -LiteralPath $Source -Destination $Destination
    Assert-Hash $Destination $Sha256
}

$pandocExe = Join-Path $RuntimeRoot "pandoc.exe"
$pandocExeSha256 = "E83F8354C0F507222B5684797B9C5AE766F03889785995D14AAC27816EC456BA"
$destinations = @($pandocExe) + @($resources | ForEach-Object { $_.Destination })
$allReady = $true
foreach ($destination in $destinations) {
    if (-not (Test-Path -LiteralPath $destination -PathType Leaf)) {
        $allReady = $false
    }
}

if ($allReady) {
    Assert-Hash $pandocExe $pandocExeSha256
    foreach ($resource in $resources) {
        Assert-Hash $resource.Destination $resource.Sha256
    }
    Write-Output "[OK] Pandoc $pandocVersion runtime is already complete and verified"
    exit 0
}

$archivePath = Join-Path $downloadRoot $archiveName
$archiveSha256 = "C97542F2800F446E788D9F74237856D995421AD1BB3CC8324286840C5F272D3A"
Get-VerifiedDownload `
    "https://github.com/jgm/pandoc/releases/download/$pandocVersion/$archiveName" `
    $archivePath $archiveSha256

New-Item -ItemType Directory -Force -Path $extractRoot | Out-Null
Expand-Archive -LiteralPath $archivePath -DestinationPath $extractRoot -Force
$extractedExe = Join-Path $extractRoot "pandoc-$pandocVersion\pandoc.exe"
Assert-Hash $extractedExe $pandocExeSha256
Copy-VerifiedFile $extractedExe $pandocExe $pandocExeSha256

foreach ($resource in $resources) {
    $downloadPath = Join-Path $downloadRoot $resource.Name
    Get-VerifiedDownload $resource.Uri $downloadPath $resource.Sha256
    Copy-VerifiedFile $downloadPath $resource.Destination $resource.Sha256
}

Write-Output "[OK] Prepared and verified Pandoc $pandocVersion runtime at $RuntimeRoot"
