@echo off
rem ============================================================
rem  Video-to-Doc 启动器（调试版）
rem
rem  保留控制台并显示完整报错，用于「双击没反应」时定位问题。
rem ============================================================
cd /d "%~dp0"
title Video-to-Doc 调试启动

echo ============================================================
echo   Video-to-Doc 调试启动
echo ============================================================
echo 当前目录: %CD%
echo.

set "PY=%~dp0venv\Scripts\python.exe"

if not exist "%PY%" (
    echo [错误] 未找到虚拟环境: %PY%
    echo 请先运行: python setup.py
    goto :end
)

echo Python: %PY%
"%PY%" --version
echo.

echo [1/2] 检查依赖能否导入...
"%PY%" -c "import sys; sys.path.insert(0, r'%~dp0'); import yaml, tkinter; print('  基础依赖 OK')"
if errorlevel 1 (
    echo   [失败] 基础依赖导入出错，见上方信息
    goto :end
)

echo [2/2] 启动图形界面（关闭窗口后本控制台会停留）...
"%PY%" "%~dp0gui.py"
echo.
echo 程序已退出，退出码: %errorlevel%

:end
echo.
pause
