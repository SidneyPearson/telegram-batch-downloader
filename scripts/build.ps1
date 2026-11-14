param([string]$Python = "$PSScriptRoot\..\.venv\Scripts\python.exe")
$ErrorActionPreference = 'Stop'
$tmdRoot = (Resolve-Path -LiteralPath "$PSScriptRoot\..").Path
Push-Location -LiteralPath $tmdRoot
try {
    & $Python scripts\run_tests.py
    if ($LASTEXITCODE -ne 0) { throw 'Tests failed. Build stopped.' }
    $tmdVersion = (& $Python -c 'import tmd; print(tmd.__version__)').Trim()
    if ($tmdVersion -notmatch '^\d+\.\d+\.\d+$') { throw 'Invalid version.' }
    $tmdDist = Join-Path $tmdRoot "dist\v$tmdVersion"
    $tmdBuild = Join-Path $tmdRoot "build\v$tmdVersion"
    foreach ($tmdPath in @($tmdDist,$tmdBuild)) {
        if (-not ([IO.Path]::GetFullPath($tmdPath)).StartsWith($tmdRoot + '\',[StringComparison]::OrdinalIgnoreCase)) { throw 'Unsafe build path.' }
    }
    & $Python -m PyInstaller --noconfirm --windowed --onedir --noupx --name TelegramBatchDownloader --distpath $tmdDist --workpath $tmdBuild --specpath $tmdBuild --version-file (Join-Path $tmdRoot 'scripts\version-info.txt') (Join-Path $tmdRoot 'app.py')
    if ($LASTEXITCODE -ne 0) { throw 'Build failed.' }
    & $Python scripts\record_build.py
    $tmdOutput = Join-Path $tmdDist 'TelegramBatchDownloader'
    Copy-Item -LiteralPath README.md,CHANGELOG.md,LICENSE,THIRD-PARTY-NOTICES.md -Destination $tmdOutput
    $tmdDocs = Join-Path $tmdOutput 'docs'
    New-Item -ItemType Directory -Path $tmdDocs -Force | Out-Null
    foreach ($tmdDocName in @('TESTING.md','MOVED-FILES.md','test-results.json','build-verification.json','pipeline-benchmark.json')) {
        $tmdDocSource = Join-Path $tmdRoot "docs\$tmdDocName"
        if (Test-Path -LiteralPath $tmdDocSource -PathType Leaf) { Copy-Item -LiteralPath $tmdDocSource -Destination $tmdDocs -Force }
    }
    Copy-Item -LiteralPath licenses -Destination $tmdOutput -Recurse -Force
    Write-Host "Built: $tmdOutput\TelegramBatchDownloader.exe"
} finally { Pop-Location }
