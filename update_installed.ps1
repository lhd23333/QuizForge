param(
    [string]$InstallDir = "",
    [switch]$SkipBuild,
    [switch]$DirectBundle,
    [ValidateRange(15, 300)]
    [int]$HealthTimeoutSeconds = 90
)

# Keep this Windows PowerShell 5.1 script ASCII-only. It is a local release
# updater: validate, build the selected release artifact, update in place,
# verify protected data, and start the installed application.
$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2.0

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$workspaceRoot = Split-Path -Parent $projectRoot
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$buildDesktop = Join-Path $projectRoot "build_desktop.ps1"
$buildInstaller = Join-Path $projectRoot "build_installer.ps1"

if (-not $InstallDir) {
    $InstallDir = Join-Path $workspaceRoot "QuizForge"
}
$InstallDir = [System.IO.Path]::GetFullPath($InstallDir)
$installedExe = Join-Path $InstallDir "QuizForge.exe"
$uninstaller = Join-Path $InstallDir "unins000.exe"

# These files are current or historical user state, never release files. Old
# activation/account files stay protected even though the open-source build no
# longer reads them.
$protectedRootNames = @(
    ".enc_key",
    "license.qflicense",
    "device_identity.dat",
    "cloud_account.json",
    "activation.json",
    "mineru.json",
    "doc2x.json",
    "doc2x_local.json",
    "providers.json",
    "service_ports.json",
    "ui_prefs.json"
)
$protectedStateNames = @("conversion_tasks.json", "selections.json")
$protectedTreeNames = @("history")
$protectedResumePatterns = @(".mineru_task_*.json", ".mineru_result_*.zip.part")

function Assert-LastExitCode([string]$Step) {
    if ($LASTEXITCODE -ne 0) {
        throw "$Step failed with exit code $LASTEXITCODE"
    }
}

function Assert-QuizForgeClosed {
    $running = @(Get-Process -Name "QuizForge" -ErrorAction SilentlyContinue)
    if ($running.Count -gt 0) {
        $pids = ($running | ForEach-Object { $_.Id }) -join ", "
        throw "QuizForge is running (PID: $pids). Close every QuizForge window before updating."
    }
}

function Assert-TextContains([string]$Path, [string]$Needle, [string]$Label) {
    $text = Get-Content -LiteralPath $Path -Raw -Encoding UTF8
    if (-not $text.Contains($Needle)) {
        throw "$Label does not match desktop_product.PRODUCT_VERSION: expected '$Needle' in $Path"
    }
}

function Assert-ExeVersion([string]$Path, [string]$ProductVersion,
                           [string]$FileVersion, [string]$Label) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "$Label executable not found: $Path"
    }
    $info = (Get-Item -LiteralPath $Path).VersionInfo
    $actualProduct = ([string]$info.ProductVersion).Trim()
    $actualFile = ([string]$info.FileVersion).Trim()
    if ($actualProduct -ne $ProductVersion -or $actualFile -ne $FileVersion) {
        throw "$Label version mismatch: product=$actualProduct file=$actualFile; expected product=$ProductVersion file=$FileVersion"
    }
}

function Get-RelativeKey([string]$Prefix, [System.IO.DirectoryInfo]$Root,
                        [System.IO.FileInfo]$File) {
    $base = $Root.FullName.TrimEnd("\")
    $relative = $File.FullName.Substring($base.Length).TrimStart("\")
    return "$Prefix|$relative"
}

function Get-CredentialSnapshot([string]$AppDataDir) {
    $snapshot = @{}
    foreach ($name in $protectedRootNames) {
        $path = Join-Path $AppDataDir $name
        $key = "appdata|$name"
        if (Test-Path -LiteralPath $path -PathType Leaf) {
            $snapshot[$key] = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash
        } else {
            $snapshot[$key] = "<missing>"
        }
    }
    return $snapshot
}

function Get-ProtectedSnapshot([string]$AppDataDir, [string]$ProgramDir,
                              [bool]$IncludeDesktopConfig,
                              [bool]$IncludeProgramResume) {
    $snapshot = @{}
    $appRoot = Get-Item -LiteralPath $AppDataDir
    $programRoot = Get-Item -LiteralPath $ProgramDir
    $names = @($protectedRootNames)
    if ($IncludeDesktopConfig) {
        $names += "desktop.json"
    }
    foreach ($name in $names) {
        $path = Join-Path $AppDataDir $name
        $key = "appdata|$name"
        if (Test-Path -LiteralPath $path -PathType Leaf) {
            $snapshot[$key] = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash
        } else {
            $snapshot[$key] = "<missing>"
        }
    }

    foreach ($name in $protectedStateNames) {
        foreach ($file in @(Get-ChildItem -LiteralPath $AppDataDir -Recurse -Force -File `
                            -Filter $name -ErrorAction SilentlyContinue)) {
            $key = Get-RelativeKey "appdata" $appRoot $file
            $snapshot[$key] = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash
        }
    }

    # History can contain large source PDFs. Re-hashing every source four times
    # during an update would make the protection check grow with the archive.
    # Path, size and mtime still detect replacement/removal, while the small
    # manifests and Markdown results receive a full content hash.
    foreach ($treeName in $protectedTreeNames) {
        foreach ($tree in @(Get-ChildItem -LiteralPath $AppDataDir -Recurse -Force `
                            -Directory -Filter $treeName `
                            -ErrorAction SilentlyContinue)) {
            foreach ($file in @(Get-ChildItem -LiteralPath $tree.FullName -Recurse `
                                -Force -File -ErrorAction SilentlyContinue)) {
                $key = Get-RelativeKey "appdata" $appRoot $file
                if ($file.Name -eq "manifest.json" -or $file.Extension -eq ".md") {
                    $snapshot[$key] = (Get-FileHash -LiteralPath $file.FullName `
                                      -Algorithm SHA256).Hash
                } else {
                    $snapshot[$key] = "$($file.Length)|$($file.LastWriteTimeUtc.Ticks)"
                }
            }
        }
    }

    $resumeRoots = @(@{ Prefix = "appdata"; Root = $appRoot })
    if ($IncludeProgramResume) {
        $resumeRoots += @{ Prefix = "program"; Root = $programRoot }
    }
    foreach ($rootInfo in $resumeRoots) {
        foreach ($pattern in $protectedResumePatterns) {
            foreach ($file in @(Get-ChildItem -LiteralPath $rootInfo.Root.FullName -Recurse `
                                -Force -File -Filter $pattern `
                                -ErrorAction SilentlyContinue)) {
                $key = Get-RelativeKey $rootInfo.Prefix $rootInfo.Root $file
                $snapshot[$key] = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash
            }
        }
    }
    return $snapshot
}

function Assert-SnapshotEqual([hashtable]$Before, [hashtable]$After,
                              [string]$Label) {
    $problems = @()
    foreach ($key in $Before.Keys) {
        if (-not $After.ContainsKey($key)) {
            $problems += "missing after update: $key"
        } elseif ($Before[$key] -ne $After[$key]) {
            $problems += "hash changed: $key"
        }
    }
    foreach ($key in $After.Keys) {
        if (-not $Before.ContainsKey($key)) {
            $problems += "new protected file: $key"
        }
    }
    if ($problems.Count -gt 0) {
        throw "$Label protected-data check failed: $($problems -join '; ')"
    }
}

function Get-RecentLoggedPorts([string]$LogPath) {
    if (-not (Test-Path -LiteralPath $LogPath -PathType Leaf)) {
        return @()
    }
    try {
        $stream = New-Object System.IO.FileStream(
            $LogPath, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read,
            [System.IO.FileShare]::ReadWrite
        )
        $reader = New-Object System.IO.StreamReader(
            $stream, [System.Text.Encoding]::UTF8
        )
        try {
            $text = $reader.ReadToEnd()
        } finally {
            $reader.Dispose()
        }
    } catch [System.IO.IOException] {
        return @()
    }
    $matches = [System.Text.RegularExpressions.Regex]::Matches(
        $text, "127\.0\.0\.1:(\d{2,5})"
    )
    $ports = New-Object System.Collections.Generic.List[int]
    for ($index = $matches.Count - 1;
         $index -ge 0 -and $ports.Count -lt 12; $index--) {
        $port = [int]$matches[$index].Groups[1].Value
        if (-not $ports.Contains($port)) {
            $ports.Add($port)
        }
    }
    return @($ports)
}

function Assert-BundleMatchesInstall([string]$BundleDir, [string]$ProgramDir) {
    $bundleRoot = Get-Item -LiteralPath $BundleDir
    $mismatches = @()
    foreach ($source in @(Get-ChildItem -LiteralPath $BundleDir -Recurse -Force -File)) {
        $relative = $source.FullName.Substring($bundleRoot.FullName.Length).TrimStart("\")
        $target = Join-Path $ProgramDir $relative
        if (-not (Test-Path -LiteralPath $target -PathType Leaf)) {
            $mismatches += "missing: $relative"
            continue
        }
        $sourceHash = (Get-FileHash -LiteralPath $source.FullName -Algorithm SHA256).Hash
        $targetHash = (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash
        if ($sourceHash -ne $targetHash) {
            $mismatches += "hash mismatch: $relative"
        }
        if ($mismatches.Count -ge 20) {
            break
        }
    }
    if ($mismatches.Count -gt 0) {
        throw "Installed program does not match the verified release bundle: $($mismatches -join '; ')"
    }
}

if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Project virtual environment not found: $python"
}
if (-not (Test-Path -LiteralPath $buildDesktop -PathType Leaf)) {
    throw "Desktop build script not found: $buildDesktop"
}
if (-not $DirectBundle -and
    -not (Test-Path -LiteralPath $buildInstaller -PathType Leaf)) {
    throw "Installer build script not found: $buildInstaller"
}
if (-not (Test-Path -LiteralPath $installedExe -PathType Leaf) -or
    -not (Test-Path -LiteralPath $uninstaller -PathType Leaf)) {
    throw "A formal QuizForge installation was not found in: $InstallDir"
}
if (-not $env:LOCALAPPDATA) {
    throw "LOCALAPPDATA is not available"
}
$appDataDir = Join-Path $env:LOCALAPPDATA "QuizForge"
if (-not (Test-Path -LiteralPath $appDataDir -PathType Container)) {
    throw "QuizForge user data directory not found: $appDataDir"
}

Assert-QuizForgeClosed

Push-Location $projectRoot
try {
    $versionOutput = & $python -c "import desktop_product; print(desktop_product.PRODUCT_VERSION)"
    Assert-LastExitCode "Reading PRODUCT_VERSION"
    $productVersion = ([string]($versionOutput | Select-Object -Last 1)).Trim()
    if ($productVersion -notmatch "^(\d+)\.(\d+)\.(\d+)(?:-[0-9A-Za-z.-]+)?$") {
        throw "Invalid desktop_product.PRODUCT_VERSION: $productVersion"
    }
    $major = [int]$Matches[1]
    $minor = [int]$Matches[2]
    $patch = [int]$Matches[3]
    $fileVersion = "$major.$minor.$patch.0"
    $versionTuple = "($major, $minor, $patch, 0)"

    Assert-TextContains (Join-Path $projectRoot "build_desktop.ps1") `
        ('[string]$Version = "' + $productVersion + '"') "Desktop product version"
    Assert-TextContains (Join-Path $projectRoot "build_desktop.ps1") `
        ('[string]$FileVersion = "' + $fileVersion + '"') "Desktop file version"
    Assert-TextContains (Join-Path $projectRoot "build_desktop.ps1") `
        '--file-version=$FileVersion' "Nuitka file version argument"
    Assert-TextContains (Join-Path $projectRoot "build_desktop.ps1") `
        '--product-version=$FileVersion' "Nuitka product version argument"
    Assert-TextContains (Join-Path $projectRoot "build_installer.ps1") `
        ('[string]$Version = "' + $productVersion + '"') "Installer product version"
    Assert-TextContains (Join-Path $projectRoot "build_installer.ps1") `
        ('[string]$FileVersion = "' + $fileVersion + '"') "Installer file version"
    Assert-TextContains (Join-Path $projectRoot "installer\QuizForge.iss") `
        ('#define MyAppVersion "' + $productVersion + '"') "Inno product version"
    Assert-TextContains (Join-Path $projectRoot "installer\QuizForge.iss") `
        ('#define MyFileVersion "' + $fileVersion + '"') "Inno file version"
    Assert-TextContains (Join-Path $projectRoot "installer\pyinstaller-version.txt") `
        ("filevers=" + $versionTuple) "PyInstaller file version"
    Assert-TextContains (Join-Path $projectRoot "installer\pyinstaller-version.txt") `
        ("prodvers=" + $versionTuple) "PyInstaller product tuple"
    Assert-TextContains (Join-Path $projectRoot "installer\pyinstaller-version.txt") `
        ("StringStruct('FileVersion', '" + $fileVersion + "')") "PyInstaller file string"
    Assert-TextContains (Join-Path $projectRoot "installer\pyinstaller-version.txt") `
        ("StringStruct('ProductVersion', '" + $productVersion + "')") "PyInstaller product string"

    $installedInfo = (Get-Item -LiteralPath $installedExe).VersionInfo
    $installedFileVersion = ([string]$installedInfo.FileVersion).Trim()
    try {
        $installedNumeric = [version]$installedFileVersion
        $targetNumeric = [version]$fileVersion
    } catch {
        throw "Installed or target file version is invalid: installed=$installedFileVersion target=$fileVersion"
    }
    if ($targetNumeric -lt $installedNumeric -or
            ($targetNumeric -eq $installedNumeric -and -not $DirectBundle)) {
        throw "Target $productVersion ($fileVersion) is not newer than installed $([string]$installedInfo.ProductVersion) ($installedFileVersion). Bump PRODUCT_VERSION before updating."
    }

    Write-Output "[1/9] Version metadata is consistent: $productVersion / $fileVersion"

    $compileFiles = @(
        "app.py", "desktop.py", "desktop_product.py", "service_ports.py",
        "tex_installer.py",
        "search_query.py", "word_exporter.py", "word_ooxml.py",
        "license_manager.py", "device_identity.py", "filestore.py", "exporter.py",
        "converter.py", "pdf_collection.py", "collection_structure.py",
        "collection_recovery.py", "ocr_pool.py",
        "mineru_store.py", "doc2x_client.py", "doc2x_store.py", "imgorder.py",
        "blockpipe.py", "blocksplit.py", "blocknorm.py", "mechfix.py", "importer.py",
        "dedup.py", "llm_client.py", "providers.py", "qrender.py", "task_store.py",
        "history_store.py",
        "cleanup_output.py", "corpus.py", "update_client.py", "tools\eval_doc2x.py",
        "vendor\project_alpha\src\mineru_client.py"
    )
    & $python -m py_compile @compileFiles
    Assert-LastExitCode "Python compile check"
    Write-Output "[2/9] Python compile check passed"

    & $python -m unittest discover -s tests -p "test_*.py"
    Assert-LastExitCode "Python unit tests"
    Write-Output "[3/9] Python unit tests passed"

    & $python -c "from app import app; [app.jinja_env.get_template(t) for t in app.jinja_env.list_templates()]"
    Assert-LastExitCode "Jinja template check"
    Write-Output "[4/9] Jinja template check passed"

    $npm = Get-Command "npm.cmd" -ErrorAction SilentlyContinue
    if ($null -eq $npm) {
        throw "npm.cmd was not found; handout frontend tests cannot run"
    }
    & $npm.Source run test:handouts
    Assert-LastExitCode "Handout frontend tests"
    Write-Output "[5/9] Handout frontend tests passed"

    if ($DirectBundle) {
        if (-not $SkipBuild) {
            & $buildDesktop -Version $productVersion -FileVersion $fileVersion
            if ($LASTEXITCODE -ne 0) {
                throw "Desktop bundle build failed with exit code $LASTEXITCODE"
            }
        } else {
            Write-Output "[6/9] Reusing the existing desktop release bundle"
        }
    } else {
        if (-not $SkipBuild) {
            & $buildInstaller -Version $productVersion -FileVersion $fileVersion
            if ($LASTEXITCODE -ne 0) {
                throw "Full installer build failed with exit code $LASTEXITCODE"
            }
        } else {
            Write-Output "[6/9] Reusing the existing release bundle and installer"
        }
    }
    $bundleExe = Join-Path $projectRoot "build\desktop\QuizForge\QuizForge.exe"
    $installerPath = Join-Path $projectRoot "build\installer\QuizForge-$productVersion-Setup.exe"
    Assert-ExeVersion $bundleExe $productVersion $fileVersion "Built bundle"
    if ($DirectBundle) {
        Write-Output "[6/9] Desktop release bundle passed version and bundle checks; no installer was built"
    } else {
        if (-not (Test-Path -LiteralPath $installerPath -PathType Leaf)) {
            throw "Built installer not found: $installerPath"
        }
        $setupInfo = (Get-Item -LiteralPath $installerPath).VersionInfo
        if (([string]$setupInfo.FileVersion).Trim() -ne $fileVersion -or
            ([string]$setupInfo.ProductVersion).Trim() -ne $fileVersion) {
            throw "Installer version resource mismatch: file=$($setupInfo.FileVersion) product=$($setupInfo.ProductVersion) expected=$fileVersion"
        }
        Write-Output "[6/9] Full installer and release bundle passed version and bundle checks"
    }

    Assert-QuizForgeClosed
    # The legacy OCR root belongs to the currently installed release. Migrate
    # every referenced workspace before the installer can replace that tree.
    $legacyOcrRoot = Join-Path $InstallDir `
        "_internal\vendor\project_alpha\output\raw_md"
    $credentialsBeforeMigration = Get-CredentialSnapshot $appDataDir
    $migrationOutput = & $python -c `
        "import pathlib, sys, desktop; print(desktop.migrate_all_legacy_ocr_workspaces(pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])))" `
        $legacyOcrRoot $appDataDir
    Assert-LastExitCode "Migrating referenced OCR workspaces before install"
    $migratedCount = ([string]($migrationOutput | Select-Object -Last 1)).Trim()
    if ($migratedCount -notmatch "^\d+$") {
        throw "OCR workspace migration returned an invalid count: $migratedCount"
    }
    $credentialsAfterMigration = Get-CredentialSnapshot $appDataDir
    Assert-SnapshotEqual $credentialsBeforeMigration $credentialsAfterMigration `
        "OCR migration"

    # Program-root resume files are the migration source and are expected to be
    # replaced by installation. Their verified copies now live in AppData.
    $protectedBefore = Get-ProtectedSnapshot $appDataDir $InstallDir $true $false
    $setupLog = ""
    if ($DirectBundle) {
        Get-ChildItem -LiteralPath (Split-Path -Parent $bundleExe) -Force |
            Copy-Item -Destination $InstallDir -Recurse -Force
    } else {
        $setupLog = Join-Path $env:TEMP ("QuizForge-update-" + [guid]::NewGuid().ToString("N") + ".log")
        $setup = New-Object System.Diagnostics.ProcessStartInfo
        $setup.FileName = $installerPath
        $setup.WorkingDirectory = Split-Path -Parent $installerPath
        $setup.UseShellExecute = $false
        $setup.CreateNoWindow = $true
        $setup.Arguments = '/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /CLOSEAPPLICATIONS /DIR="' + `
            $InstallDir + '" /LOG="' + $setupLog + '"'
        $setupProcess = [System.Diagnostics.Process]::Start($setup)
        if ($null -eq $setupProcess) {
            throw "Failed to start the installer"
        }
        if (-not $setupProcess.WaitForExit(600000)) {
            try { $setupProcess.Kill() } catch {}
            throw "Installer timed out after 10 minutes. Log: $setupLog"
        }
        if ($setupProcess.ExitCode -ne 0) {
            throw "Installer failed with exit code $($setupProcess.ExitCode). Log: $setupLog"
        }
    }

    Assert-ExeVersion $installedExe $productVersion $fileVersion "Installed application"
    Assert-BundleMatchesInstall (Split-Path -Parent $bundleExe) $InstallDir
    $protectedAfterInstall = Get-ProtectedSnapshot $appDataDir $InstallDir $true $false
    Assert-SnapshotEqual $protectedBefore $protectedAfterInstall "Post-install"
    # Active task states were rejected before migration, so application startup
    # must not need to normalize pending/converting/in-flight state. Keep an
    # exact hash boundary across health startup.
    $stableBefore = Get-ProtectedSnapshot $appDataDir $InstallDir $false $true
    $updateKind = if ($DirectBundle) { "direct bundle copy" } else { "installer" }
    Write-Output "[7/9] In-place update completed via $updateKind; protected data verified; migrated OCR workspaces: $migratedCount"

    $appLog = Join-Path $appDataDir "logs\quizforge.log"
    $started = Start-Process -FilePath $installedExe -WorkingDirectory $InstallDir -PassThru
    $deadline = [DateTime]::UtcNow.AddSeconds($HealthTimeoutSeconds)
    $health = $null
    $lastPort = $null
    while ([DateTime]::UtcNow -lt $deadline) {
        foreach ($candidatePort in @(Get-RecentLoggedPorts $appLog)) {
            try {
                $candidate = Invoke-RestMethod `
                    -Uri "http://127.0.0.1:$candidatePort/healthz" `
                    -Method Get -TimeoutSec 2
                $candidateProcess = Get-Process -Id ([int]$candidate.pid) `
                    -ErrorAction SilentlyContinue
                if ($candidate.app -eq "quizforge" -and $candidate.status -eq "ok" -and
                    $null -ne $candidateProcess -and
                    $candidateProcess.Path -eq $installedExe) {
                    $health = $candidate
                    $lastPort = $candidatePort
                    break
                }
            } catch {
                # Recent log ports can belong to already-closed windows.
            }
        }
        if ($null -ne $health) {
            break
        }
        if ($started.HasExited -and
            @(Get-Process -Name "QuizForge" -ErrorAction SilentlyContinue).Count -eq 0) {
            throw "Updated QuizForge exited before becoming healthy. Log: $appLog"
        }
        Start-Sleep -Milliseconds 500
    }
    if ($null -eq $health) {
        throw "Updated QuizForge did not pass /healthz within $HealthTimeoutSeconds seconds. Last port: $lastPort. Log: $appLog"
    }
    Write-Output "[8/9] Updated application is healthy on 127.0.0.1:$lastPort (PID $($health.pid))"

    $stableAfterHealth = Get-ProtectedSnapshot $appDataDir $InstallDir $false $true
    Assert-SnapshotEqual $stableBefore $stableAfterHealth "Post-start"
    Write-Output "[9/9] Credentials, cloud account, license, task state, and MinerU resume files remain unchanged"
    Write-Output "[OK] QuizForge was updated in place to $productVersion"
    if (-not $DirectBundle) {
        Write-Output "[OK] Installer log: $setupLog"
    }
} finally {
    Pop-Location
}
