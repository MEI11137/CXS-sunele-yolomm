@echo off
chcp 936 >nul
setlocal EnableExtensions
title 海巡智检 v1.8 - WorkBuddy 云端连接

rem ---------------------------------------------------------------------------
rem 本文件只启动「本机连接器」，把云端入口的 /api 请求转给本机后端。
rem * 后端由 启动海巡智检.cmd 单独启动，同一个 data-dir，本文件不碰数据库；
rem * 路径全部相对本文件所在目录，可以带中文和空格，不含任何开发机绝对路径；
rem * 不打印、不写死 .tunnel-token；也不使用第二个令牌；
rem * 退出码：成功/自检通过=0；自检失败沿用 preflight 的 2/3/4/5；连接器失败沿用它的退出码。
rem ---------------------------------------------------------------------------

set "HX17_ROOT=%~dp0"
if "%HX17_ROOT:~-1%"=="\" set "HX17_ROOT=%HX17_ROOT:~0,-1%"
set "HX17_GW=%HX17_ROOT%\deploy\workbuddy"

rem 云端入口：默认沿用当前固定分享链接；要换入口时设 HX17_PUBLIC
if not defined HX17_PUBLIC set "HX17_PUBLIC=https://haixun-inspection.app.workbuddy.host/?sharecode=YHJNHOFRKT"
rem 本机后端：默认 8080；端口不同时设 HX17_UPSTREAM，或把新地址作为第一个参数
if not defined HX17_UPSTREAM set "HX17_UPSTREAM=http://127.0.0.1:8080"
if not "%~1"=="" set "HX17_UPSTREAM=%~1"

rem Node：优先 HX17_NODE（显式路径），其次 PATH 里的 node
set "HX17_NODE_BIN="
if defined HX17_NODE if exist "%HX17_NODE%" set "HX17_NODE_BIN=%HX17_NODE%"
if not defined HX17_NODE_BIN for /f "delims=" %%I in ('where node 2^>nul') do if not defined HX17_NODE_BIN set "HX17_NODE_BIN=%%I"

if not defined HX17_NODE_BIN goto :no_node
if not exist "%HX17_GW%\connector.js" goto :no_gateway

echo [1/2] 检查 Node、隧道令牌与本机后端...
"%HX17_NODE_BIN%" "%HX17_GW%\preflight.mjs" --upstream "%HX17_UPSTREAM%"
set "HX17_EXIT=%ERRORLEVEL%"
if not "%HX17_EXIT%"=="0" goto :preflight_failed

if /i "%HX17_DRY_RUN%"=="1" goto :dry_run

echo.
echo [2/2] 启动云端连接（关闭本窗口即断开；本机后端继续运行）
echo       入口 %HX17_PUBLIC%
echo       后端 %HX17_UPSTREAM%
echo.
"%HX17_NODE_BIN%" "%HX17_GW%\connector.js" --public "%HX17_PUBLIC%" --upstream "%HX17_UPSTREAM%" --lanes 4
set "HX17_EXIT=%ERRORLEVEL%"
echo.
if "%HX17_EXIT%"=="0" goto :connector_stopped
echo 连接器已退出，退出码 %HX17_EXIT%。
echo 常见原因：网络不通、令牌与云端不匹配、云端入口已变更或该应用已重发布。
goto :connector_failed

:dry_run
echo.
echo [dry-run] 条件都通过；按 HX17_DRY_RUN=1 不启动连接器。
echo [dry-run] 入口 %HX17_PUBLIC%
echo [dry-run] 后端 %HX17_UPSTREAM%
goto :ok

:connector_stopped
echo 连接器已正常停止。网页此时会提示巡检服务器未连接，重新双击本文件即可恢复。
goto :ok

:no_node
echo [错误] 没有找到 Node。
echo 处理：安装 Node 22 LTS，或设置 HX17_NODE 为 node.exe 的完整路径后重试。
echo 说明：后端本身不需要 Node；WorkBuddy 云端连接器需要 Node 18 及以上。
set "HX17_EXIT=1"
goto :failed

:no_gateway
echo [错误] 缺少 deploy\workbuddy\connector.js。
echo 处理：确认在完整源码目录里运行；git clone 后不要删除 deploy\workbuddy。
set "HX17_EXIT=1"
goto :failed

:preflight_failed
echo.
echo 启动前检查未通过（退出码 %HX17_EXIT%）：连接器没有启动，也没有向云端发起任何连接。
echo 处理：按上面 [preflight] 的提示补齐 Node、令牌或本机后端，然后重新双击本文件。
goto :failed

:connector_failed
echo.
echo 本次启动/运行失败（退出码 %HX17_EXIT%）：当前没有可用连接，网页入口不会因此变正常。
echo 处理：看上面连接器打印的 [hx17] 行；本机后端不因此停止。
goto :failed

:failed
echo.
pause
endlocal & exit /b %HX17_EXIT%

:ok
endlocal & exit /b 0
