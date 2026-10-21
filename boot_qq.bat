@echo off
chcp 65001 >nul
rem ============================================================
rem  qq-fish-bot: start QQ + NapCat only (no fishbot)
rem  used by watchdog.py when the OneBot port 3001 is gone
rem ============================================================
rem ★改成你自己的 NapCat 目录（NapCatWinBootMain.exe 那层）
set "NAPCAT_DIR=C:\NapCat\shell"
rem 项目目录 = 本脚本所在目录，一般不用改
for %%I in ("%~dp0.") do set "BOT_DIR=%%~fI"
rem ★改成机器人小号的 QQ 号
set "BOT_UIN=123456789"
set "QQ_EXE=C:\Program Files\Tencent\QQNT\QQ.exe"
set "BOOTLOG=%BOT_DIR%\data\boot.log"

echo [%date% %time%] boot_qq start >> "%BOOTLOG%"
taskkill /f /im QQ.exe >nul 2>&1
ping -n 4 127.0.0.1 >nul 2>&1

set "NAPCAT_PATCH_PACKAGE=%NAPCAT_DIR%\qqnt.json"
set "NAPCAT_LOAD_PATH=%NAPCAT_DIR%\loadNapCat.js"
set "NAPCAT_INJECT_PATH=%NAPCAT_DIR%\NapCatWinBootHook.dll"
set "NAPCAT_LAUNCHER_PATH=%NAPCAT_DIR%\NapCatWinBootMain.exe"
set "NAPCAT_MAIN_PATH=%NAPCAT_DIR%\napcat.mjs"
cd /d "%NAPCAT_DIR%"
start "NapCat" /min "%NAPCAT_LAUNCHER_PATH%" "%QQ_EXE%" "%NAPCAT_INJECT_PATH%" %BOT_UIN%

set /a tries=0
:waitport
ping -n 4 127.0.0.1 >nul 2>&1
set /a tries+=1
netstat -ano | findstr /c:"127.0.0.1:3001" | findstr /c:"LISTENING" >nul
if not errorlevel 1 goto :ready
if %tries% lss 20 goto :waitport
echo [%date% %time%] boot_qq: port 3001 never came up >> "%BOOTLOG%"
exit /b 1

:ready
echo [%date% %time%] boot_qq: port 3001 ok after %tries% tries >> "%BOOTLOG%"
exit /b 0
