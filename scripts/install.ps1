param([switch]$NoStart)
$ErrorActionPreference = 'Stop'
$env:PYTHONUTF8 = '1'
Set-Location (Split-Path $PSScriptRoot -Parent)
$uvBin = Join-Path (Get-Location) '.uv\uv.exe'
if (!(Test-Path $uvBin)) {
    $existing = Get-Command uv -ErrorAction SilentlyContinue
    if ($existing) { $uvBin = $existing.Source } else {
        New-Item -ItemType Directory -Force .uv | Out-Null
        Invoke-WebRequest 'https://astral.sh/uv/install.ps1' -OutFile .uv\install.ps1
        $previousDir = $env:UV_INSTALL_DIR
        $previousPath = $env:UV_NO_MODIFY_PATH
        try {
            $env:UV_INSTALL_DIR = Join-Path (Get-Location) '.uv'
            $env:UV_NO_MODIFY_PATH = '1'
            & .\.uv\install.ps1
        } finally {
            $env:UV_INSTALL_DIR = $previousDir
            $env:UV_NO_MODIFY_PATH = $previousPath
        }
    }
}
& $uvBin sync --locked --no-dev --python 3.11
if ($LASTEXITCODE -ne 0) { throw 'Workbench installation failed' }
& $uvBin run --no-sync speakvideo doctor
if ($LASTEXITCODE -ne 0) { throw 'Configuration check failed' }
if (!$NoStart) { & $uvBin run --no-sync speakvideo serve; exit $LASTEXITCODE }
