@echo off
rem ============================================================
rem  Video-to-Doc 启动器（.bat 版本）
rem
rem  为什么还需要这个文件：
rem  启动.vbs 依赖 Windows 对 .vbs 的文件关联。如果那项关联被安全软件
rem  或系统优化工具改动过，双击 启动.vbs 会弹出"打开方式"对话框而不是
rem  启动程序。.bat 不依赖关联，双击即可运行。
rem ============================================================
chcp 65001 >nul
cd /d "%~dp0"

if not exist "venv\Scripts\pythonw.exe" (
    echo.
    echo  [错误] 未找到虚拟环境：venv\Scripts\pythonw.exe
    echo         请先在项目目录运行:  python setup.py
    echo.
    pause
    exit /b 1
)

rem pythonw.exe 启动时不会弹出黑色控制台窗口
start "" "venv\Scripts\pythonw.exe" "gui.py"

exit /b 0
