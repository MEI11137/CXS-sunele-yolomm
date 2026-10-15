@echo off
setlocal EnableExtensions

title PV Defect Detection - One-click Launcher

set "ROOT=%~dp0"
for %%I in ("%ROOT%..\..") do set "WORKSPACE_ROOT=%%~fI"
set "BACKEND_SCRIPT=%ROOT%start-backend.cmd"
set "FRONTEND_SCRIPT=%ROOT%start-frontend.cmd"
set "PYTHON_EXE=D:\Anaconda3\envs\py12\python.exe"
set "NODE_EXE=%WORKSPACE_ROOT%\.tools\node-v22.2.0-win-x64\node.exe"
set "NPM_CMD=%WORKSPACE_ROOT%\.tools\node-v22.2.0-win-x64\npm.cmd"
set "BACKEND_URL=http://127.0.0.1:5020/api/health/health_check"
set "FRONTEND_URL=http://127.0.0.1:3000/"

call :require_file "%BACKEND_SCRIPT%"
if errorlevel 1 goto :dependency_error
call :require_file "%FRONTEND_SCRIPT%"
if errorlevel 1 goto :dependency_error
call :require_file "%PYTHON_EXE%"
if errorlevel 1 goto :dependency_error
call :require_file "%NODE_EXE%"
if errorlevel 1 goto :dependency_error
call :require_file "%NPM_CMD%"
if errorlevel 1 goto :dependency_error

if /I "%~1"=="--check" (
    echo CHECK_OK
    exit /b 0
)

echo [1/3] Starting services...
call :url_ready "%BACKEND_URL%"
if errorlevel 1 (
    echo       Starting backend on port 5020...
    start "PV Defect Detection - Backend" "%ComSpec%" /k call "%BACKEND_SCRIPT%"
) else (
    echo       Backend is already running.
)

call :url_ready "%FRONTEND_URL%"
if errorlevel 1 (
    echo       Starting frontend on port 3000...
    start "PV Defect Detection - Frontend" "%ComSpec%" /k call "%FRONTEND_SCRIPT%"
) else (
    echo       Frontend is already running.
)

echo [2/3] Waiting for the system to become ready (up to 60 seconds)...
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$deadline=(Get-Date).AddSeconds(60); $targets=@('%BACKEND_URL%','%FRONTEND_URL%'); foreach($target in $targets){$ready=$false; while((Get-Date)-lt $deadline){try{$response=Invoke-WebRequest -UseBasicParsing -Uri $target -TimeoutSec 2; if($response.StatusCode -eq 200){$ready=$true; break}}catch{}; Start-Sleep -Milliseconds 500}; if(-not $ready){Write-Host ('[ERROR] Service did not become ready: '+$target) -ForegroundColor Red; exit 1}}; exit 0"
if errorlevel 1 goto :startup_error

echo [3/3] System is ready: http://localhost:3000/
if not defined ONE_CLICK_NO_BROWSER start "" "http://localhost:3000/"
echo.
echo Keep the backend and frontend windows open while using the system.
exit /b 0

:require_file
if exist "%~1" exit /b 0
echo [ERROR] Required file was not found:
echo         %~1
exit /b 1

:url_ready
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "try{$response=Invoke-WebRequest -UseBasicParsing -Uri '%~1' -TimeoutSec 2; if($response.StatusCode -eq 200){exit 0}}catch{}; exit 1"
exit /b %errorlevel%

:dependency_error
echo.
echo The launcher cannot continue because a required dependency is missing.
echo Please keep this file inside the photovoltaic-panels-detect-web folder.
pause
exit /b 1

:startup_error
echo.
echo Startup failed. Check the backend and frontend windows for details.
pause
exit /b 1
