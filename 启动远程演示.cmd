@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0deploy\start-demo.ps1" %*
if errorlevel 1 pause
