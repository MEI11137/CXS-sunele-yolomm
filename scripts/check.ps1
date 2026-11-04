# 本机检查：后端单元测试 + 前端类型检查与构建。
# 用法：powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\check.ps1
param(
    [string]$Python = $env:HAIXUN_PYTHON,
    [string]$Node = $env:HAIXUN_NODE
)

$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
Set-Location -LiteralPath $root

if (-not $Python) { $Python = 'python' }
if (-not $Node) { $Node = 'node' }

# 本机若残留指向不存在文件的 NODE_OPTIONS，会让 node/npm 直接启动失败；
# 只在本脚本进程内清空，不修改用户环境变量。
if ($env:NODE_OPTIONS) {
    Write-Host "NOTE: clearing inherited NODE_OPTIONS for this run" -ForegroundColor Yellow
    $env:NODE_OPTIONS = ''
}

Write-Host "== backend tests ==" -ForegroundColor Cyan
& $Python -m unittest discover -s backend/tests -t . -v
if ($LASTEXITCODE -ne 0) { throw 'backend tests failed' }

Write-Host "== frontend typecheck + build ==" -ForegroundColor Cyan
Push-Location frontend
try {
    $env:PATH = (Split-Path -Parent (Get-Command $Node).Source) + ';' + $env:PATH
    npm run typecheck
    if ($LASTEXITCODE -ne 0) { throw 'frontend typecheck failed' }
    npm run test:logic
    if ($LASTEXITCODE -ne 0) { throw 'frontend logic tests failed' }
    npm run build
    if ($LASTEXITCODE -ne 0) { throw 'frontend build failed' }
} finally {
    Pop-Location
}

Write-Host "ALL CHECKS PASSED" -ForegroundColor Green
