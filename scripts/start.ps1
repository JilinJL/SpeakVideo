$ErrorActionPreference = 'Stop'
$env:PYTHONUTF8 = '1'
Set-Location (Split-Path $PSScriptRoot -Parent)
$uvBin = Join-Path (Get-Location) '.uv\uv.exe'
if (!(Test-Path $uvBin)) {
    $existing = Get-Command uv -ErrorAction SilentlyContinue
    if (!$existing) { & "$PSScriptRoot\install.ps1"; exit $LASTEXITCODE }
    $uvBin = $existing.Source
}
if (!(Test-Path '.venv\Scripts\python.exe')) { & "$PSScriptRoot\install.ps1"; exit $LASTEXITCODE }
& $uvBin run --no-sync speakvideo serve
exit $LASTEXITCODE
