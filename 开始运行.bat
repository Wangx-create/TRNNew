@echo off
:: 设置编码为 UTF-8 防止中文乱码
chcp 65001 >nul

set "GIT_EXE=C:\Program Files\Git\bin\sh.exe"
if not exist "%GIT_EXE%" set "GIT_EXE=D:\Program Files\Git\bin\sh.exe"

echo ----------------------------------------------------
echo 正在通过 Git 环境启动热搜雷达，请稍候...
echo ----------------------------------------------------

"%GIT_EXE%" --login -i -c "cd /d/TRNNew && PYTHONIOENCODING=utf-8 /d/python/python-3.12/python.exe -c \"import sys; sys.path.append('.'); import trendradar.__main__; trendradar.__main__.main()\""

echo.
echo ----------------------------------------------------
echo 任务执行完毕！
pause