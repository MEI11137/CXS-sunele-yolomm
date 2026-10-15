$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
$originalPath = $env:PATH
$cleanPath = "$env:SystemRoot\System32;$env:SystemRoot;$env:SystemRoot\System32\WindowsPowerShell\v1.0"
$stdoutLog = [IO.Path]::GetTempFileName()
$stderrLog = [IO.Path]::GetTempFileName()
$frontendProcess = $null

function Test-Frontend {
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:3000/' -TimeoutSec 2
        return $response.StatusCode -eq 200
    }
    catch {
        return $false
    }
}

try {
    if (Get-NetTCPConnection -State Listen -LocalPort 3000 -ErrorAction SilentlyContinue) {
        throw 'Port 3000 must be free before running this test.'
    }

    $env:PATH = $cleanPath
    $arguments = '/d /s /c call "start-frontend.cmd"'
    $frontendProcess = Start-Process -FilePath $env:ComSpec -ArgumentList $arguments `
        -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput $stdoutLog -RedirectStandardError $stderrLog

    $deadline = (Get-Date).AddSeconds(20)
    while ((Get-Date) -lt $deadline -and -not $frontendProcess.HasExited -and -not (Test-Frontend)) {
        Start-Sleep -Milliseconds 500
    }

    if (-not (Test-Frontend)) {
        $stdout = Get-Content -Raw $stdoutLog -ErrorAction SilentlyContinue
        $stderr = Get-Content -Raw $stderrLog -ErrorAction SilentlyContinue
        throw "Frontend did not start without a global Node PATH entry. ExitCode=$($frontendProcess.ExitCode)`nSTDOUT:`n$stdout`nSTDERR:`n$stderr"
    }

    Write-Host 'PASS: frontend starts with the bundled Node even when global Node is absent from PATH.'
}
finally {
    $env:PATH = $originalPath
    $listenerIds = @(
        Get-NetTCPConnection -State Listen -LocalPort 3000 -ErrorAction SilentlyContinue |
            Select-Object -ExpandProperty OwningProcess -Unique
    )
    foreach ($processId in $listenerIds) {
        Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
    }
    if ($null -ne $frontendProcess -and -not $frontendProcess.HasExited) {
        Stop-Process -Id $frontendProcess.Id -Force -ErrorAction SilentlyContinue
    }
    Remove-Item $stdoutLog, $stderrLog -Force -ErrorAction SilentlyContinue
}
