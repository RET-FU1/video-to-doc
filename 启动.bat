@echo off
rem ============================================================
rem  Video-to-Doc 启动器（.bat）
rem
rem  用 pythonw.exe 启动，不会出现黑色控制台窗口。
rem  不依赖 .vbs 的文件关联。
rem  如果双击没有任何反应，请改用「启动（调试）.bat」，它会显示具体错误。
rem  更稳妥的方式：双击同目录下的「视频转文档.lnk」快捷方式。
rem ============================================================
cd /d "%~dp0"

set "PYW=%~dp0venv\Scripts\pythonw.exe"
set "PY=%~dp0venv\Scripts\python.exe"
set "GUI=%~dp0gui.py"

if not exist "%PYW%" (
    echo.
    echo  [错误] 未找到虚拟环境: %PYW%
    echo         请先在项目目录运行:  python setup.py
    echo.
    pause
    exit /b 1
)

if not exist "%GUI%" (
    echo.
    echo  [错误] 未找到 gui.py: %GUI%
    echo.
    pause
    exit /b 1
)

echo 正在启动 Video-to-Doc ...

rem 先用控制台版自检一次，能在启动阶段就暴露导入错误
"%PY%" -c "import sys; sys.path.insert(0, r'%~dp0'); import gui" 2>nul
if errorlevel 1 (
    echo.
    echo  [启动失败] 程序自检未通过，请改用「启动（调试）.bat」查看详细错误。
    echo.
    pause
    exit /b 1
)

start "" "%PYW%" "%GUI%"
exit /b 0
