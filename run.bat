@echo off
chcp 65001 >nul 2>&1
setlocal
cd /d "%~dp0"

echo ==========================================
echo   Chi Bai Fan De Fei Yu - QQ AI Bot
echo ==========================================
echo.

where python >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found in PATH.
    echo         Install Python 3.10+ and tick "Add python.exe to PATH".
    echo.
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo [1/3] Creating virtual environment...
    python -m venv .venv
    if errorlevel 1 (
        echo [ERROR] Failed to create virtual environment.
        pause
        exit /b 1
    )
) else (
    echo [1/3] Virtual environment ready.
)

echo [2/3] Installing dependencies...
".venv\Scripts\python.exe" -m pip install -q --upgrade pip
".venv\Scripts\python.exe" -m pip install -q -r requirements.txt
if errorlevel 1 (
    echo [ERROR] Dependency install failed. Check network or proxy settings.
    pause
    exit /b 1
)

echo [3/3] Starting fishbot ...
echo.
".venv\Scripts\python.exe" -m fishbot %*

echo.
echo fishbot exited with code %errorlevel%
pause
