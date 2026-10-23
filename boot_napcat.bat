@echo off
chcp 65001 >nul
rem ============================================================
rem  qq-fish-bot: start the standalone NapCat (OneBot ws 3001)
rem
rem  "standalone" = NapCat's own bundled QQ runtime
rem  (<NapCat 目录>\shell_node: node.exe + wrapper.node + QQNT.dll),
rem  so it does NOT need the user's QQ.exe and never touches it.
rem
rem  2026-10-23: the old injection way (NapCatWinBootMain.exe into
rem  C:\Program Files\Tencent\QQNT\QQ.exe) stopped working - QQ's main
rem  process exits right after injection and 3001 never comes back.
rem  Also: NapCat must be created detached (WMI), otherwise it dies when
rem  the launching console closes - hence start_napcat_wmi.ps1.
rem ============================================================
set "BOT_DIR=%~dp0"
set "BOOTLOG=%BOT_DIR%data\boot.log"

if not exist "%BOT_DIR%data" mkdir "%BOT_DIR%data"
echo [%date% %time%] boot_napcat start >> "%BOOTLOG%"

netstat -ano | findstr /c:"127.0.0.1:3001" | findstr /c:"LISTENING" >nul
if not errorlevel 1 (
  echo [%date% %time%] boot_napcat: 3001 already listening, nothing to do >> "%BOOTLOG%"
  exit /b 0
)

if not exist "%BOT_DIR%start_napcat_wmi.ps1" (
  echo [%date% %time%] start_napcat_wmi.ps1 missing >> "%BOOTLOG%"
  exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%BOT_DIR%start_napcat_wmi.ps1" >> "%BOOTLOG%" 2>&1
set "RC=%ERRORLEVEL%"
echo [%date% %time%] boot_napcat finished rc=%RC% >> "%BOOTLOG%"
exit /b %RC%
