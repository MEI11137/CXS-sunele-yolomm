@echo off
setlocal
set "PYTHON_RESOLVER=%~dp0resolve-python.ps1"

if not exist "%PYTHON_RESOLVER%" (
    echo [ERROR] Python resolver was not found:
    echo         %PYTHON_RESOLVER%
    exit /b 1
)

set "PYTHON_EXE="
for /f "usebackq delims=" %%I in (`powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%PYTHON_RESOLVER%"`) do if not defined PYTHON_EXE set "PYTHON_EXE=%%I"
if not defined PYTHON_EXE (
    echo [ERROR] A compatible Python environment could not be resolved.
    exit /b 1
)

if /I "%~1"=="--check" (
    echo PYTHON_OK: %PYTHON_EXE%
    exit /b 0
)

set "YOLO_CONFIG_DIR=%~dp0.ultralytics"
cd /d "%~dp0web-flask"
"%PYTHON_EXE%" app.py
exit /b %errorlevel%

