bat
@echo off
chcp 65001 >nul

set "PYTHON_EXE=D:\python\python-3.12\python.exe"
if not exist "%PYTHON_EXE%" set "PYTHON_EXE=python"

echo ----------------------------------------------------
echo 启动 TrendRadar 配置中心 ...
echo ----------------------------------------------------

"%PYTHON_EXE%" config_ui_server.py

echo.
echo ----------------------------------------------------
echo 配置中心已退出。
pause