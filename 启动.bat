@echo off
cd /d "%~dp0"

if not exist "venv\Scripts\python.exe" (
    echo 未找到虚拟环境，请先运行: python setup.py
    pause
    exit /b 1
)

start "" "venv\Scripts\pythonw.exe" "gui.py"
