[CmdletBinding()]
param([switch]$SetupLocal,[switch]$Check)
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
try {
    $python = $env:HAIXUN_PYTHON
    if (!$python -or !(Test-Path -LiteralPath $python -PathType Leaf)) { throw 'Set HAIXUN_PYTHON to the python.exe in your existing YOLO environment first.' }
    if (!$env:HX_ENGINE_BIN) {
        foreach ($base in @($root,(Split-Path $root -Parent))) {
            $candidate = Join-Path $base '.runtime\hxengine\hxengine.exe'
            if (Test-Path -LiteralPath $candidate -PathType Leaf) { $env:HX_ENGINE_BIN=$candidate; break }
        }
    }
    if (!$env:HX_ENGINE_BIN) {
        $binary = Get-Command hxengine.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($binary) { $env:HX_ENGINE_BIN=$binary.Source }
    }
    Set-Location -LiteralPath $root
    $arguments = @((Join-Path $PSScriptRoot 'serve.py'))
    if ($SetupLocal) { $arguments += '--setup-local' }
    if ($Check) { $arguments += '--check' }
    & $python @arguments
    exit $LASTEXITCODE
} catch { Write-Host ('DEPLOY_FAILED: '+$_.Exception.Message) -ForegroundColor Red; exit 1 }
