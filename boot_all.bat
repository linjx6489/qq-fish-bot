@echo off
chcp 65001 >nul
rem ============================================================
rem  qq-fish-bot auto boot: NapCat (standalone) + fishbot, no pause
rem  called by the Startup folder shortcut at logon
rem
rem  2026-10-23: the old way injected NapCatWinBootMain.exe into the
rem  user's QQ.exe (C:\Program Files\Tencent\QQNT\QQ.exe) and killed
rem  QQ.exe first. That path stopped working, and killing QQ.exe logs
rem  the user's own account out. Now we use the standalone Node build
rem  (<NapCat 目录>\shell_node), which never touches the user's QQ.
rem ============================================================
rem 项目目录 = 本脚本所在目录，一般不用改
for %%I in ("%~dp0.") do set "BOT_DIR=%%~fI"
rem ★改成机器人小号的 QQ 号
set "BOT_UIN=123456789"
set "BOOTLOG=%BOT_DIR%\data\boot.log"

if not exist "%BOT_DIR%\data" mkdir "%BOT_DIR%\data"
echo [%date% %time%] boot_all start >> "%BOOTLOG%"

if not exist "%BOT_DIR%\boot_napcat.bat" (
  echo [%date% %time%] boot_napcat.bat missing, skip NapCat >> "%BOOTLOG%"
  goto :bot
)
echo [%date% %time%] starting NapCat (standalone node build) >> "%BOOTLOG%"
call "%BOT_DIR%\boot_napcat.bat"

:bot
if not exist "%BOT_DIR%\.venv\Scripts\python.exe" (
  echo [%date% %time%] venv missing, abort >> "%BOOTLOG%"
  exit /b 1
)
echo [%date% %time%] starting fishbot >> "%BOOTLOG%"
cd /d "%BOT_DIR%"
start "fishbot" /min cmd /c ".venv\Scripts\python.exe -m fishbot >> fishbot.log 2>&1"
echo [%date% %time%] boot_all done >> "%BOOTLOG%"
exit /b 0
