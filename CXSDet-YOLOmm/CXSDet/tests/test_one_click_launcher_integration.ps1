$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
$launcherName = ([char]0x4E00) + ([char]0x952E) + ([char]0x542F) + ([char]0x52A8) + '.cmd'
$launcher = Join-Path $projectRoot $launcherName
$processIdsBeforeTest = @(Get-CimInstance Win32_Process | Select-Object -ExpandProperty ProcessId)

function Test-Url([string] $Url) {
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri $Url -TimeoutSec 2
        return $response.StatusCode -eq 200
    }
    catch {
        return $false
    }
}

function Assert-NoListener([int[]] $Ports) {
    $listeners = @(Get-NetTCPConnection -State Listen -LocalPort $Ports -ErrorAction SilentlyContinue)
    if ($listeners.Count -gt 0) {
        $details = $listeners | Select-Object LocalAddress, LocalPort, OwningProcess | Format-Table -AutoSize | Out-String
        throw "The integration test requires free ports $($Ports -join ', '):`n$details"
    }
}

function Stop-TestProcesses {
    $listenerProcessIds = @(
        Get-NetTCPConnection -State Listen -LocalPort 3000, 5020 -ErrorAction SilentlyContinue |
            Select-Object -ExpandProperty OwningProcess -Unique
    )

    $projectProcessIds = @(
        Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
            Where-Object {
                $_.ProcessId -notin $processIdsBeforeTest -and
                $null -ne $_.CommandLine -and
                $_.CommandLine.IndexOf($projectRoot, [StringComparison]::OrdinalIgnoreCase) -ge 0
            } |
            Select-Object -ExpandProperty ProcessId
    )

    foreach ($processId in @($listenerProcessIds + $projectProcessIds | Select-Object -Unique)) {
        Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
    }
}

function Invoke-Launcher {
    $launcherProcess = Start-Process -FilePath $launcher -WindowStyle Hidden -PassThru
    if (-not $launcherProcess.WaitForExit(70000)) {
        Stop-Process -Id $launcherProcess.Id -Force -ErrorAction SilentlyContinue
        throw 'Launcher did not exit within 70 seconds.'
    }
    if ($launcherProcess.ExitCode -ne 0) {
        throw "Launcher failed with exit code $($launcherProcess.ExitCode)."
    }
}

try {
    Assert-NoListener @(3000, 5020)
    $env:ONE_CLICK_NO_BROWSER = '1'

    Invoke-Launcher

    if (-not (Test-Url 'http://127.0.0.1:5020/api/health/health_check')) {
        throw 'Backend health endpoint is not reachable after cold start.'
    }

    if (-not (Test-Url 'http://127.0.0.1:3000/')) {
        throw 'Frontend is not reachable after cold start.'
    }

    $listenerIdsBeforeSecondRun = @(
        Get-NetTCPConnection -State Listen -LocalPort 3000, 5020 |
            Sort-Object LocalPort |
            Select-Object -ExpandProperty OwningProcess
    )
    Invoke-Launcher
    $listenerIdsAfterSecondRun = @(
        Get-NetTCPConnection -State Listen -LocalPort 3000, 5020 |
            Sort-Object LocalPort |
            Select-Object -ExpandProperty OwningProcess
    )

    if (($listenerIdsBeforeSecondRun -join ',') -ne ($listenerIdsAfterSecondRun -join ',')) {
        throw 'Second launcher run replaced or duplicated a running service.'
    }

    Write-Host 'PASS: launcher cold-started both services, verified readiness, and avoided duplicate startup.'
}
finally {
    Remove-Item Env:ONE_CLICK_NO_BROWSER -ErrorAction SilentlyContinue
    Stop-TestProcesses
}
