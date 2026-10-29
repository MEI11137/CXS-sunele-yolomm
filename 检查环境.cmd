@echo off
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" -Check %*
if errorlevel 1 pause
pause
