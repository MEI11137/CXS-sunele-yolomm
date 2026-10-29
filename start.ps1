[CmdletBinding()]
param([switch]$Check, [switch]$Stop, [switch]$NoBrowser, [ValidateSet('All','Backend','Frontend')][string]$Service='All', [int]$BackendPort=5020, [int]$FrontendPort=5173)
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$appDir = Join-Path $root 'CXSDet-YOLOmm\CXSDet'
$backDir = Join-Path $appDir 'web-flask'
$frontDir = Join-Path $appDir 'web-haixun'
$appFile = Join-Path $backDir 'app.py'
$viteFile = Join-Path $frontDir 'node_modules\vite\bin\vite.js'
$runDir = Join-Path $root '.run'
$stateFile = Join-Path $runDir 'services.json'

function Listener([int]$port) {
    return [bool]([Net.NetworkInformation.IPGlobalProperties]::GetIPGlobalProperties().GetActiveTcpListeners() | Where-Object Port -eq $port)
}
function Owned([int]$port, [string]$entry) {
    $socket = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if (!$socket) { return $null }
    $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$($socket.OwningProcess)"
    if ($proc.CommandLine -and $proc.CommandLine.Contains($entry)) { return $proc }
    return $null
}
function StopOwned([int]$port, [string]$entry) {
    if (!(Listener $port)) { return }
    $proc = Owned $port $entry
    if (!$proc) { throw "Port $port belongs to another version/program. It was NOT stopped." }
    $parent = Get-CimInstance Win32_Process -Filter "ProcessId=$($proc.ParentProcessId)" -ErrorAction SilentlyContinue
    Stop-Process -Id $proc.ProcessId -ErrorAction Stop
    if ($parent -and $parent.CommandLine -and $parent.CommandLine.Contains($entry)) { Stop-Process -Id $parent.ProcessId -ErrorAction SilentlyContinue }
}

if ($Stop) {
    if (Test-Path -LiteralPath $stateFile) {
        $state = Get-Content -LiteralPath $stateFile -Raw | ConvertFrom-Json
        $BackendPort = [int]$state.backendPort; $FrontendPort = [int]$state.frontendPort
    }
    StopOwned $FrontendPort $viteFile
    StopOwned $BackendPort $appFile
    Write-Host 'HaiXun v1.1 services stopped. Files and data are preserved.'
    exit 0
}

try {
    if ($BackendPort -lt 1024 -or $BackendPort -gt 65535 -or $FrontendPort -lt 1024 -or $FrontendPort -gt 65535 -or $BackendPort -eq $FrontendPort) { throw 'Invalid service ports.' }
    function ResolveRuntime([string]$configured, [string[]]$candidates, [string]$commandName) {
        if ($configured) {
            if (!(Test-Path -LiteralPath $configured -PathType Leaf)) { throw "Configured runtime does not exist: $configured" }
            return (Resolve-Path -LiteralPath $configured).Path
        }
        foreach ($candidate in $candidates) {
            if (Test-Path -LiteralPath $candidate -PathType Leaf) { return (Resolve-Path -LiteralPath $candidate).Path }
        }
        $command = Get-Command $commandName -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($command) { return $command.Source }
        throw "Runtime missing: $commandName. See README.md installation steps."
    }
    $localPackage = Split-Path $root -Parent
    $python = ResolveRuntime $env:HAIXUN_PYTHON @((Join-Path $root '.venv\Scripts\python.exe'),(Join-Path $env:USERPROFILE '.workbuddy\binaries\python\envs\pvdetect\Scripts\python.exe')) 'python.exe'
    $node = ResolveRuntime $env:HAIXUN_NODE @((Join-Path $root '.runtime\node\node.exe'),(Join-Path $localPackage '.runtime\node\node.exe')) 'node.exe'
    $openCode = ResolveRuntime $env:OPENCODE_BIN @((Join-Path $root '.runtime\opencode\opencode.exe'),(Join-Path $localPackage '.runtime\opencode\opencode.exe')) 'opencode.exe'
    foreach ($file in @($appFile,$viteFile)) {
        if (!(Test-Path -LiteralPath $file -PathType Leaf)) { throw "Required file missing: $file. See README.md; install Python dependencies and run npm ci in web-haixun." }
    }
    $env:PYTHONPATH = (Join-Path $backDir 'vendor') + [IO.Path]::PathSeparator + $env:PYTHONPATH
    & $python -c "import importlib.util,sys; names=('flask','torch','PIL','cryptography','ultralytics'); missing=[x for x in names if importlib.util.find_spec(x) is None]; print('Missing modules: '+str(missing)) if missing else None; sys.exit(bool(missing))"
    if ($LASTEXITCODE -ne 0) { throw 'Python dependency check failed.' }
    & $node --version
    & $openCode --version
    if ($LASTEXITCODE -ne 0) { throw 'OpenCode check failed.' }
    if ($Check) { Write-Host "CHECK_OK: HaiXun v1.1; Python=$python; frontend=web-haixun"; exit 0 }
    $targets = @()
    if ($Service -ne 'Frontend') { $targets += [pscustomobject]@{ Port=$BackendPort; Entry=$appFile } }
    if ($Service -ne 'Backend') { $targets += [pscustomobject]@{ Port=$FrontendPort; Entry=$viteFile } }
    foreach ($item in $targets) {
        if ((Listener $item.Port) -and !(Owned $item.Port $item.Entry)) { throw "Port $($item.Port) is occupied by another version. Stop the old version, or pass -BackendPort and -FrontendPort. Nothing was stopped." }
    }
    New-Item -ItemType Directory -Path $runDir -Force | Out-Null
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $env:FLASK_SERVICE_HOST = '127.0.0.1'
    $env:FLASK_SERVICE_PORT = [string]$BackendPort
    $env:DB_PATH = Join-Path $backDir 'yyxz_sqlite.db'
    $env:AGENT_SECRET_PATH = Join-Path $backDir 'agent_secret.key'
    $env:FILE_STORE_PATH = Join-Path $backDir 'file_store'
    $env:FLASK_TEMP_DIR = Join-Path $backDir 'temp'
    $env:FILE_PUBLIC_URL = "http://127.0.0.1:$BackendPort/api/file"
    $env:FRONTEND_ORIGINS = "http://127.0.0.1:$FrontendPort,http://localhost:$FrontendPort"
    $env:OPENCODE_BIN = $openCode
    $env:AGENT_ENGINE = 'opencode'
    $env:OPENCODE_BRIDGE_ORIGIN = "http://127.0.0.1:$BackendPort"
    $env:YOLO_CONFIG_DIR = Join-Path $runDir 'ultralytics'
    $env:PATH = (Split-Path $node) + ';' + $env:PATH
    $env:VITE_BACKEND_ORIGIN = "http://127.0.0.1:$BackendPort"
    $started = @()
    try {
        if ($Service -ne 'Frontend' -and !(Listener $BackendPort)) {
            $null = Start-Process $python -ArgumentList ('"' + $appFile + '"') -WorkingDirectory $backDir -WindowStyle Hidden -RedirectStandardOutput (Join-Path $runDir "backend-$stamp.out.log") -RedirectStandardError (Join-Path $runDir "backend-$stamp.err.log") -PassThru
            $started += [pscustomobject]@{ Port=$BackendPort; Entry=$appFile }
        }
        if ($Service -ne 'Backend' -and !(Listener $FrontendPort)) {
            $null = Start-Process $node -ArgumentList @(('"' + $viteFile + '"'),'--host','127.0.0.1','--port',[string]$FrontendPort,'--strictPort') -WorkingDirectory $frontDir -WindowStyle Hidden -RedirectStandardOutput (Join-Path $runDir "frontend-$stamp.out.log") -RedirectStandardError (Join-Path $runDir "frontend-$stamp.err.log") -PassThru
            $started += [pscustomobject]@{ Port=$FrontendPort; Entry=$viteFile }
        }
        $deadline = (Get-Date).AddSeconds(55)
        $urls = @()
        if ($Service -ne 'Frontend') { $urls += "http://127.0.0.1:$BackendPort/api/health/health_check" }
        if ($Service -ne 'Backend') { $urls += "http://127.0.0.1:$FrontendPort/" }
        foreach ($url in $urls) {
            $ready = $false
            while ((Get-Date) -lt $deadline) {
                try { $response = Invoke-WebRequest -UseBasicParsing -Uri $url -TimeoutSec 2; if ($response.StatusCode -eq 200) { $ready=$true; break } } catch { }
                Start-Sleep -Milliseconds 400
            }
            if (!$ready) { throw "Service not ready: $url. See $runDir logs." }
        }
        @{version='1.1';backendPort=$BackendPort;frontendPort=$FrontendPort;startedAt=(Get-Date).ToString('o')} | ConvertTo-Json | Set-Content -LiteralPath $stateFile -Encoding UTF8
    } catch {
        foreach ($item in $started) { try { StopOwned $item.Port $item.Entry } catch {} }
        throw
    }
    Write-Host "READY: http://127.0.0.1:$FrontendPort/ (v1.1). Logs: $runDir"
    if (!$NoBrowser -and $Service -ne 'Backend') { Start-Process "http://127.0.0.1:$FrontendPort/" }
} catch {
    Write-Host ("START_FAILED: " + $_.Exception.Message) -ForegroundColor Red
    exit 1
}
