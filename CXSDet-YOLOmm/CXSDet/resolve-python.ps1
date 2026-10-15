[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$requiredModules = @('flask', 'torch', 'ultralytics')
$preferredEnvironment = if ([string]::IsNullOrWhiteSpace($env:CXSDET_CONDA_ENV)) {
    'py12'
}
else {
    $env:CXSDET_CONDA_ENV.Trim()
}

function Test-CompatiblePython([string] $Path) {
    if ([string]::IsNullOrWhiteSpace($Path)) {
        return $false
    }

    $candidate = [Environment]::ExpandEnvironmentVariables($Path.Trim().Trim('"'))
    if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) {
        return $false
    }

    $probe = "import importlib.util, struct, sys; required=('flask','torch','ultralytics'); missing=[name for name in required if importlib.util.find_spec(name) is None]; sys.exit(0 if struct.calcsize('P') * 8 == 64 and not missing else 1)"
    $previousErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = 'SilentlyContinue'
    try {
        & $candidate -c $probe *> $null
        $probeExitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    return $probeExitCode -eq 0
}

function Write-ResolvedPython([string] $Path) {
    $resolved = (Resolve-Path -LiteralPath $Path).Path
    [Console]::Out.WriteLine($resolved)
    exit 0
}

if (-not [string]::IsNullOrWhiteSpace($env:CXSDET_PYTHON)) {
    if (Test-CompatiblePython $env:CXSDET_PYTHON) {
        Write-ResolvedPython $env:CXSDET_PYTHON
    }

    [Console]::Error.WriteLine(
        'CXSDET_PYTHON does not point to a compatible 64-bit Python with Flask, Torch, and Ultralytics: {0}',
        $env:CXSDET_PYTHON
    )
    exit 2
}

$candidates = New-Object 'System.Collections.Generic.List[string]'
$seen = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)

function Add-Candidate([string] $Path) {
    if ([string]::IsNullOrWhiteSpace($Path)) {
        return
    }

    $candidate = [Environment]::ExpandEnvironmentVariables($Path.Trim().Trim('"'))
    if ($seen.Add($candidate)) {
        $candidates.Add($candidate)
    }
}

foreach ($prefix in @($env:CONDA_PREFIX, $env:VIRTUAL_ENV)) {
    if (-not [string]::IsNullOrWhiteSpace($prefix)) {
        Add-Candidate (Join-Path $prefix 'python.exe')
    }
}

$registeredEnvironments = New-Object 'System.Collections.Generic.List[string]'
$registryFiles = New-Object 'System.Collections.Generic.List[string]'
if (-not [string]::IsNullOrWhiteSpace($env:USERPROFILE)) {
    $registryFiles.Add((Join-Path $env:USERPROFILE '.conda\environments.txt'))
}
if (-not [string]::IsNullOrWhiteSpace($env:CONDA_ENVS_PATH)) {
    foreach ($environmentRoot in ($env:CONDA_ENVS_PATH -split ';')) {
        if (-not [string]::IsNullOrWhiteSpace($environmentRoot)) {
            $registeredEnvironments.Add($environmentRoot.Trim())
        }
    }
}

foreach ($registryFile in $registryFiles) {
    if (Test-Path -LiteralPath $registryFile -PathType Leaf) {
        foreach ($environmentRoot in (Get-Content -LiteralPath $registryFile -ErrorAction SilentlyContinue)) {
            if (-not [string]::IsNullOrWhiteSpace($environmentRoot)) {
                $registeredEnvironments.Add($environmentRoot.Trim())
            }
        }
    }
}

$orderedEnvironments = $registeredEnvironments |
    Sort-Object `
        @{ Expression = { if ((Split-Path -Leaf $_) -ieq $preferredEnvironment) { 0 } else { 1 } } }, `
        @{ Expression = { $_ } }

foreach ($environmentRoot in $orderedEnvironments) {
    Add-Candidate (Join-Path $environmentRoot 'python.exe')
}

foreach ($pythonCommand in @(Get-Command python.exe -All -ErrorAction SilentlyContinue)) {
    Add-Candidate $pythonCommand.Source
}

$pyLauncher = Get-Command py.exe -ErrorAction SilentlyContinue
if ($null -ne $pyLauncher) {
    foreach ($line in @(& $pyLauncher.Source -0p 2>$null)) {
        if ($line -match '([A-Za-z]:\\.*python\.exe)\s*$') {
            Add-Candidate $Matches[1]
        }
    }
}

foreach ($candidate in $candidates) {
    if (Test-CompatiblePython $candidate) {
        Write-ResolvedPython $candidate
    }
}

[Console]::Error.WriteLine('No compatible 64-bit Python environment was found.')
[Console]::Error.WriteLine('Required modules: {0}', ($requiredModules -join ', '))
[Console]::Error.WriteLine('Preferred Conda environment name: {0}', $preferredEnvironment)
[Console]::Error.WriteLine('Set CXSDET_PYTHON to a compatible python.exe, or set CXSDET_CONDA_ENV to the desired Conda environment name.')
exit 1
