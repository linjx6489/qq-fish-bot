@echo off
chcp 65001 >nul
setlocal
title QQ Fish Bot - STOP

rem 项目目录 = 本脚本所在目录，一般不用改
for %%I in ("%~dp0.") do set "BOT_DIR=%%~fI"

echo ==================================================
echo   QQ Fish Bot   PAUSE BUTTON
echo ==================================================
echo.
echo  This will:
echo    1. pause the watchdog scheduled task
echo    2. stop the bot and the WeChat bridge
echo.
echo  It will NOT touch NapCat or your QQ / WeChat.
echo.

cd /d "%BOT_DIR%" || (
  echo [ERROR] bot dir not found: %BOT_DIR%
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] venv missing
  pause
  exit /b 1
)

".venv\Scripts\python.exe" watchdog.py --stop
echo.
pause
exit /b 0
