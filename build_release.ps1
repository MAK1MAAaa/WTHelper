$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$buildPython = Join-Path $PSScriptRoot '.venv-build\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $buildPython)) {
    throw 'Create .venv-build and install requirements-build.txt first.'
}
& $buildPython -m PyInstaller --noconfirm --clean --onefile --console --name WTHelper --paths . --add-data 'licenses;licenses' --add-data 'LICENSE;licenses' scripts/packaged_main.py
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
