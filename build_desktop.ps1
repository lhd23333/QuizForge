param(
    [ValidateSet("auto", "pyinstaller", "nuitka")]
    [string]$Engine = "auto",
    [string]$OutputDir = "build\desktop"
)

# Keep this PowerShell 5.1 script ASCII-only. Windows PowerShell reads a UTF-8
# file without BOM using the system code page and may corrupt quoted strings.
$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    throw "Project virtual environment not found: $python"
}

$icon = Join-Path $projectRoot "assets\quizforge.ico"
& $python (Join-Path $projectRoot "tools\build_app_icon.py")
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $icon)) {
    throw "Application icon generation failed"
}

$resolvedOutput = [System.IO.Path]::GetFullPath((Join-Path $projectRoot $OutputDir))
New-Item -ItemType Directory -Force -Path $resolvedOutput | Out-Null

$pythonVersion = & $python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
$hasMsvc = $null -ne (Get-Command cl.exe -ErrorAction SilentlyContinue)
if ($Engine -eq "auto") {
    # Nuitka cannot use MinGW with Python 3.13+. Prefer it when a compatible
    # compiler exists; otherwise produce the runnable prototype with PyInstaller.
    if ($hasMsvc -or ([version]$pythonVersion -lt [version]"3.13")) {
        $Engine = "nuitka"
    } else {
        $Engine = "pyinstaller"
    }
}

$oldPythonPath = $env:PYTHONPATH
$alphaRoot = Join-Path $projectRoot "vendor\project_alpha"
$env:PYTHONPATH = if ($oldPythonPath) { "$alphaRoot;$oldPythonPath" } else { $alphaRoot }
$runtime = Join-Path $projectRoot "runtime"
$runtimeFiles = @()
if (Test-Path -LiteralPath $runtime) {
    $runtimeFiles = @(Get-ChildItem -LiteralPath $runtime -File -Recurse -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -ne "README.md" })
}

try {
    if ($Engine -eq "nuitka") {
        $compilerArg = if ($hasMsvc) { "--msvc=latest" } else { "--mingw64" }
        $args = @(
            "-m", "nuitka",
            "--mode=standalone",
            $compilerArg,
            "--assume-yes-for-downloads",
            "--windows-console-mode=disable",
            "--windows-icon-from-ico=$icon",
            "--file-version=0.17.0.0",
            "--product-version=0.17.0.0",
            "--enable-plugin=tk-inter",
            "--include-package=src",
            "--include-package=webview",
            "--include-data-dir=$projectRoot\templates=templates",
            "--include-data-dir=$projectRoot\static=static",
            "--include-data-dir=$projectRoot\prompts=prompts",
            "--include-data-dir=$projectRoot\vendor\project_alpha\templates=vendor/project_alpha/templates",
            "--include-data-files=$projectRoot\exam_template.tex=exam_template.tex",
            "--include-data-files=$projectRoot\assets\license_public_key.pem=assets/license_public_key.pem",
            "--include-data-files=$projectRoot\assets\wimath-logo-latex-black.pdf=assets/wimath-logo-latex-black.pdf",
            "--output-dir=$resolvedOutput",
            "--output-filename=QuizForge.exe",
            "--product-name=QuizForge",
            "--file-description=QuizForge local question bank and exam builder",
            "$projectRoot\desktop.py"
        )
        if ($runtimeFiles.Count -gt 0) {
            $args = $args[0..($args.Count - 2)] +
                @("--include-data-dir=$runtime=runtime", $args[-1])
        }
        & $python @args
        $dist = Join-Path $resolvedOutput "desktop.dist"
    } else {
        $work = Join-Path $projectRoot "build\pyinstaller-work"
        $spec = Join-Path $projectRoot "build\pyinstaller-spec"
        $args = @(
            "-m", "PyInstaller",
            "--noconfirm",
            "--clean",
            "--onedir",
            "--windowed",
            "--name", "QuizForge",
            "--icon", $icon,
            "--version-file", (Join-Path $projectRoot "installer\pyinstaller-version.txt"),
            "--paths", $alphaRoot,
            "--collect-submodules", "src",
            "--collect-all", "webview",
            "--add-data", "$projectRoot\templates;templates",
            "--add-data", "$projectRoot\static;static",
            "--add-data", "$projectRoot\prompts;prompts",
            "--add-data", "$projectRoot\vendor\project_alpha\templates;vendor/project_alpha/templates",
            "--add-data", "$projectRoot\exam_template.tex;.",
            "--add-data", "$projectRoot\assets\license_public_key.pem;assets",
            "--add-data", "$projectRoot\assets\wimath-logo-latex-black.pdf;assets",
            "--distpath", $resolvedOutput,
            "--workpath", $work,
            "--specpath", $spec,
            "$projectRoot\desktop.py"
        )
        if ($runtimeFiles.Count -gt 0) {
            $args = $args[0..($args.Count - 2)] +
                @("--add-data", "$runtime;runtime", $args[-1])
        }
        & $python @args
        $dist = Join-Path $resolvedOutput "QuizForge"
    }
    if ($LASTEXITCODE -ne 0) {
        throw "$Engine build failed with exit code $LASTEXITCODE"
    }
} finally {
    $env:PYTHONPATH = $oldPythonPath
}

if (-not (Test-Path -LiteralPath (Join-Path $dist "QuizForge.exe"))) {
    throw "QuizForge.exe not found after build: $dist"
}
& $python (Join-Path $projectRoot "tools\verify_desktop_bundle.py") --dist $dist
if ($LASTEXITCODE -ne 0) {
    throw "Desktop release bundle verification failed"
}
Write-Output "[OK] Engine: $Engine"
Write-Output "[OK] Desktop distribution: $dist"
