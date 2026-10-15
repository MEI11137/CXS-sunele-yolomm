@echo off
setlocal
set "YOLO_CONFIG_DIR=%~dp0.ultralytics"
cd /d "%~dp0web-flask"
"D:\Anaconda3\envs\py12\python.exe" app.py

