$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
$resolver = Join-Path $projectRoot 'resolve-python.ps1'
$backendLauncher = Join-Path $projectRoot 'start-backend.cmd'
$oneClickLauncherName = ([char]0x4E00) + ([char]0x952E) + ([char]0x542F) + ([char]0x52A8) + '.cmd'
$oneClickLauncher = Join-Path $projectRoot $oneClickLauncherName
$originalPythonOverride = $env:CXSDET_PYTHON
$originalCondaPreference = $env:CXSDET_CONDA_ENV

function Invoke-Resolver {
    $previousErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $output = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $resolver 2>&1
        $resolverExitCode = $LASTEXITCODE
        return [pscustomobject]@{
            ExitCode = $resolverExitCode
            Output = @($output)
            Text = ($output -join [Environment]::NewLine)
        }
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
}

try {
    if (-not (Test-Path -LiteralPath $resolver -PathType Leaf)) {
        throw "Expected dynamic Python resolver was not found: $resolver"
    }

    foreach ($launcher in @($backendLauncher, $oneClickLauncher)) {
        $content = Get-Content -Raw -LiteralPath $launcher
        if ($content -match '(?i)[A-Z]:\\[^\r\n"]*python\.exe') {
            throw "Launcher contains a machine-specific absolute Python path: $launcher"
        }
    }

    Remove-Item Env:CXSDET_PYTHON -ErrorAction SilentlyContinue
    Remove-Item Env:CXSDET_CONDA_ENV -ErrorAction SilentlyContinue
    $automatic = Invoke-Resolver
    if ($automatic.ExitCode -ne 0) {
        throw "Automatic Python resolution failed:`n$($automatic.Text)"
    }

    $resolvedPython = $automatic.Output | Select-Object -Last 1
    if (-not (Test-Path -LiteralPath $resolvedPython -PathType Leaf)) {
        throw "Resolver returned a path that does not exist: $resolvedPython"
    }

    $env:CXSDET_PYTHON = $resolvedPython
    $explicit = Invoke-Resolver
    if ($explicit.ExitCode -ne 0) {
        throw "Explicit CXSDET_PYTHON resolution failed:`n$($explicit.Text)"
    }
    if (($explicit.Output | Select-Object -Last 1) -ne $resolvedPython) {
        throw 'CXSDET_PYTHON did not take precedence over automatic discovery.'
    }

    $env:CXSDET_PYTHON = Join-Path ([IO.Path]::GetTempPath()) 'missing-cxsdet-python.exe'
    $invalid = Invoke-Resolver
    if ($invalid.ExitCode -eq 0) {
        throw 'An invalid CXSDET_PYTHON override unexpectedly succeeded.'
    }
    if ($invalid.Text -notmatch 'CXSDET_PYTHON') {
        throw "Invalid override error did not identify CXSDET_PYTHON:`n$($invalid.Text)"
    }

    $env:CXSDET_PYTHON = $resolvedPython
    $backendCheck = & $env:ComSpec /d /c ('call "{0}" --check' -f $backendLauncher) 2>&1
    if ($LASTEXITCODE -ne 0 -or ($backendCheck -join [Environment]::NewLine) -notmatch 'PYTHON_OK') {
        throw "Backend launcher self-check failed:`n$($backendCheck -join [Environment]::NewLine)"
    }

    Write-Host "PASS: backend Python resolves dynamically without a machine-specific path ($resolvedPython)."
}
finally {
    if ($null -eq $originalPythonOverride) {
        Remove-Item Env:CXSDET_PYTHON -ErrorAction SilentlyContinue
    }
    else {
        $env:CXSDET_PYTHON = $originalPythonOverride
    }

    if ($null -eq $originalCondaPreference) {
        Remove-Item Env:CXSDET_CONDA_ENV -ErrorAction SilentlyContinue
    }
    else {
        $env:CXSDET_CONDA_ENV = $originalCondaPreference
    }
}
