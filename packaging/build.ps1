# Build the Windows distribution: dist\dayloom\ with dayloom.exe + dayloom-gui.exe
#
#   pwsh -File packaging\build.ps1
#
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Push-Location $root
try {
    $python = Join-Path $root ".venv\Scripts\python.exe"
    if (-not (Test-Path $python)) { throw "先创建虚拟环境：python -m venv .venv" }

    Write-Host "installing build deps..." -ForegroundColor Cyan
    & $python -m pip install --quiet --upgrade pyinstaller
    & $python -m pip install --quiet -e ".[qt]"

    Write-Host "building..." -ForegroundColor Cyan
    & $python -m PyInstaller --noconfirm --clean --distpath (Join-Path $root "dist") `
        --workpath (Join-Path $root "build") packaging\dayloom.spec
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller 失败（exit $LASTEXITCODE）" }

    $out = Join-Path $root "dist\dayloom"
    Write-Host "done: $out" -ForegroundColor Green
    Get-ChildItem $out -Filter *.exe | ForEach-Object {
        Write-Host ("  {0}  {1:N1} MB" -f $_.Name, ($_.Length / 1MB))
    }
}
finally {
    Pop-Location
}
