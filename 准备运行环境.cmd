@echo off
chcp 936 >nul
setlocal EnableExtensions
title 准备海巡智检运行环境
cd /d "%~dp0"

rem 只需在首次部署或更新代码后运行；日常启动不用重复执行。
set "PYTHON=%HX17_PYTHON%"
if not defined PYTHON if exist "%~dp0.venv\Scripts\python.exe" set "PYTHON=%~dp0.venv\Scripts\python.exe"
if not defined PYTHON set "PYTHON=python"

"%PYTHON%" -c "import sys; raise SystemExit(0 if sys.version_info>=(3,10) else 1)" >nul 2>nul
if errorlevel 1 (
  echo [错误] 没找到可用的 Python 3.10 及以上版本，请先安装 Python。
  pause
  exit /b 1
)

echo 将安装后端依赖并构建前端（需要能访问 pip / npm 源，环境已有 Node 22.6+）。
echo 视觉依赖（torch 等）默认不装：需要真实推理时用参数 --with-vision 再运行本脚本。
echo.
"%PYTHON%" "%~dp0deploy\serve.py" prepare %*
set "CODE=%ERRORLEVEL%"
echo.
if "%CODE%"=="0" (
  echo 准备完成。之后双击「启动海巡智检.cmd」即可。
) else (
  echo [退出码 %CODE%] 准备没有完成，请按上面的提示处理。
)
pause
exit /b %CODE%
