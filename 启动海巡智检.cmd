@echo off
chcp 936 >nul
setlocal EnableExtensions
title 海巡智检 v1.7
cd /d "%~dp0"

rem 解释器来源：HX17_PYTHON > 项目内 .venv > PATH 里的 python。
set "PYTHON=%HX17_PYTHON%"
if not defined PYTHON if exist "%~dp0.venv\Scripts\python.exe" set "PYTHON=%~dp0.venv\Scripts\python.exe"
if not defined PYTHON set "PYTHON=python"

"%PYTHON%" -c "import sys; raise SystemExit(0 if sys.version_info>=(3,10) else 1)" >nul 2>nul
if errorlevel 1 (
  echo [错误] 没找到可用的 Python 3.10 及以上版本。
  echo   1^) 安装 Python 3.10+，安装时勾选 Add Python to PATH；
  echo   2^) 或在项目文件夹创建 .venv；
  echo   3^) 或设置环境变量 HX17_PYTHON 指向 python.exe。
  echo.
  pause
  exit /b 1
)

echo 正在启动海巡智检（首次启动会自动建库，稍等几秒）……
"%PYTHON%" "%~dp0deploy\serve.py" start %*
set "CODE=%ERRORLEVEL%"

if not "%CODE%"=="0" (
  echo.
  echo [退出码 %CODE%] 没有启动成功，请按上面的提示处理。
  pause
)
exit /b %CODE%
