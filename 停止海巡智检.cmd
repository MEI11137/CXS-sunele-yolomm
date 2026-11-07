@echo off
chcp 936 >nul
setlocal EnableExtensions
title 停止海巡智检
cd /d "%~dp0"

rem 解释器来源：HX17_PYTHON > 项目内 .venv > PATH 里的 python。
set "PYTHON=%HX17_PYTHON%"
if not defined PYTHON if exist "%~dp0.venv\Scripts\python.exe" set "PYTHON=%~dp0.venv\Scripts\python.exe"
if not defined PYTHON set "PYTHON=python"

"%PYTHON%" -c "import sys; raise SystemExit(0 if sys.version_info>=(3,10) else 1)" >nul 2>nul
if errorlevel 1 (
  echo [错误] 没找到可用的 Python 3.10 及以上版本，无法读取运行记录。
  echo   1^) 先双击「准备运行环境.cmd」；或在项目文件夹创建 .venv；
  echo   2^) 或设置环境变量 HX17_PYTHON 指向 python.exe。
  echo.
  pause
  exit /b 1
)

echo 正在停止本数据目录记录的海巡智检实例……
"%PYTHON%" "%~dp0deploy\serve.py" stop %*
set "CODE=%ERRORLEVEL%"
echo.
echo [退出码 %CODE%] 0=已停止或本来没在运行；4=记录属于别的数据目录；6=进程还在但不响应；7=身份不符；9=还有进行中的任务；10=任务状态读不出来。
if not "%CODE%"=="0" echo            以上都不是 0：没有结束任何进程，请按上面的原因处理。
pause
exit /b %CODE%
