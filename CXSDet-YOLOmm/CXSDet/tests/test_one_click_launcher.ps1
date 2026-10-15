$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
$launcherName = ([char]0x4E00) + ([char]0x952E) + ([char]0x542F) + ([char]0x52A8) + '.cmd'
$launcher = Join-Path $projectRoot $launcherName
$commandLine = '""{0}" --check"' -f $launcher

$output = & $env:ComSpec /d /c $commandLine 2>&1
$exitCode = $LASTEXITCODE

if ($exitCode -ne 0) {
    throw "Launcher self-check failed (exit code $exitCode):`n$($output -join [Environment]::NewLine)"
}

if (($output -join [Environment]::NewLine) -notmatch 'CHECK_OK') {
    throw "Launcher self-check did not return CHECK_OK:`n$($output -join [Environment]::NewLine)"
}

Write-Host 'PASS: one-click launcher is executable and all required dependencies are ready.'
