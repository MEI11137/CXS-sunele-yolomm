@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0..\..\start.ps1" -Service Frontend %*
if errorlevel 1 pause
