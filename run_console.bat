@echo off
chcp 65001 >nul 2>&1
setlocal
cd /d "%~dp0"

echo ==========================================
echo   Fei Yu - Local Console Mode
echo   Type messages to chat, /quit to exit.
echo ==========================================
echo.

where python >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found in PATH.
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo Preparing virtual environment...
    python -m venv .venv
    ".venv\Scripts\python.exe" -m pip install -q --upgrade pip
)

".venv\Scripts\python.exe" -m pip install -q -r requirements.txt

".venv\Scripts\python.exe" -m fishbot --simulate
echo.
pause
