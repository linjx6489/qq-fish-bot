@echo off
chcp 65001 >nul
rem ============================================================
rem  qq-fish-bot auto boot: NapCat + fishbot, no pause
rem  called by the Startup folder shortcut at logon
rem ============================================================
rem ★改成你自己的 NapCat 目录（NapCatWinBootMain.exe 那层）
set "NAPCAT_DIR=C:\NapCat\shell"
rem 项目目录 = 本脚本所在目录，一般不用改
for %%I in ("%~dp0.") do set "BOT_DIR=%%~fI"
rem ★改成机器人小号的 QQ 号
set "BOT_UIN=123456789"
set "QQ_EXE=C:\Program Files\Tencent\QQNT\QQ.exe"
set "BOOTLOG=%BOT_DIR%\data\boot.log"

if not exist "%BOT_DIR%\data" mkdir "%BOT_DIR%\data"
echo [%date% %time%] boot_all start >> "%BOOTLOG%"

if not exist "%NAPCAT_DIR%\NapCatWinBootMain.exe" (
  echo [%date% %time%] NapCat missing, skip >> "%BOOTLOG%"
  goto :bot
)
if not exist "%QQ_EXE%" (
  echo [%date% %time%] QQ missing, skip >> "%BOOTLOG%"
  goto :bot
)

echo [%date% %time%] killing old QQ >> "%BOOTLOG%"
taskkill /f /im QQ.exe >nul 2>&1
ping -n 4 127.0.0.1 >nul 2>&1

set "NAPCAT_PATCH_PACKAGE=%NAPCAT_DIR%\qqnt.json"
set "NAPCAT_LOAD_PATH=%NAPCAT_DIR%\loadNapCat.js"
set "NAPCAT_INJECT_PATH=%NAPCAT_DIR%\NapCatWinBootHook.dll"
set "NAPCAT_LAUNCHER_PATH=%NAPCAT_DIR%\NapCatWinBootMain.exe"
set "NAPCAT_MAIN_PATH=%NAPCAT_DIR%\napcat.mjs"
echo [%date% %time%] starting NapCat >> "%BOOTLOG%"
cd /d "%NAPCAT_DIR%"
start "NapCat" /min "%NAPCAT_LAUNCHER_PATH%" "%QQ_EXE%" "%NAPCAT_INJECT_PATH%" %BOT_UIN%

echo [%date% %time%] waiting for port 3001 >> "%BOOTLOG%"
set /a tries=0
:waitport
ping -n 4 127.0.0.1 >nul 2>&1
set /a tries+=1
netstat -ano | findstr /c:"127.0.0.1:3001" | findstr /c:"LISTENING" >nul
if not errorlevel 1 goto :ready
if %tries% lss 20 goto :waitport
echo [%date% %time%] port 3001 never came up >> "%BOOTLOG%"

:ready
echo [%date% %time%] port 3001 ok after %tries% tries >> "%BOOTLOG%"

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
