@echo off
setlocal
for %%I in ("%~dp0..\..") do set "WORKSPACE_ROOT=%%~fI"
set "NODE_HOME=%WORKSPACE_ROOT%\.tools\node-v22.2.0-win-x64"
set "PATH=%NODE_HOME%;%PATH%"
cd /d "%~dp0web-vue"
"%NODE_HOME%\npm.cmd" run dev

