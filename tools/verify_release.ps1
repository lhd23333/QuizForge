param(
    [switch]$SkipBundleScan,
    [string]$Dist = "build\desktop\QuizForge"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2.0

$projectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"

function Invoke-Checked([string]$Label, [scriptblock]$Command) {
    & $Command
    if ($LASTEXITCODE -ne 0) {
        throw "$Label failed with exit code $LASTEXITCODE"
    }
    Write-Output "[OK] $Label"
}

if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Project virtual environment not found: $python"
}

Push-Location $projectRoot
try {
    $compileFiles = @(
        "app.py", "desktop.py", "desktop_product.py", "service_ports.py",
        "license_manager.py", "device_identity.py", "filestore.py", "exporter.py",
        "converter.py", "pdf_collection.py", "collection_structure.py",
        "collection_recovery.py", "ocr_pool.py", "mineru_store.py",
        "doc2x_client.py", "doc2x_store.py", "imgorder.py", "blockpipe.py",
        "blocksplit.py", "blocknorm.py", "mechfix.py", "importer.py", "dedup.py",
        "llm_client.py", "providers.py", "qrender.py", "task_store.py",
        "cleanup_output.py", "corpus.py", "tools\eval_doc2x.py",
        "vendor\project_alpha\src\mineru_client.py"
    )
    Invoke-Checked "Python compile check" {
        & $python -m py_compile @compileFiles
    }
    Invoke-Checked "Python unit tests" {
        & $python -m unittest discover -s tests -p "test_*.py"
    }
    Invoke-Checked "Jinja template check" {
        & $python -c "from app import app; [app.jinja_env.get_template(t) for t in app.jinja_env.list_templates()]"
    }

    $node = Get-Command "node.exe" -ErrorAction SilentlyContinue
    if ($null -eq $node) {
        throw "node.exe was not found"
    }
    $jsFiles = @(
        Get-ChildItem -LiteralPath "static\js", "frontend" -Filter "*.js" `
            -Recurse -File | Where-Object {
                $_.FullName -notlike "*\frontend\tests\*"
            }
    )
    foreach ($file in $jsFiles) {
        Invoke-Checked "JavaScript syntax: $($file.FullName.Substring($projectRoot.Length + 1))" {
            & $node.Source --check $file.FullName
        }
    }

    $npm = Get-Command "npm.cmd" -ErrorAction SilentlyContinue
    if ($null -eq $npm) {
        throw "npm.cmd was not found"
    }
    Invoke-Checked "Frontend tests" {
        & $npm.Source run test:handouts
    }

    if (-not $SkipBundleScan) {
        $distPath = [System.IO.Path]::GetFullPath((Join-Path $projectRoot $Dist))
        Invoke-Checked "Release bundle scan" {
            & $python tools\verify_desktop_bundle.py --dist $distPath
        }
    }
} finally {
    Pop-Location
}

Write-Output "[OK] QuizForge release verification completed"
