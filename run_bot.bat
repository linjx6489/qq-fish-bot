@echo off
rem run fishbot headless with a sanitized environment
rem called by watchdog.py through WMI so it survives the scheduled task
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
set http_proxy=
set https_proxy=
set all_proxy=
set no_proxy=
set HTTP_PROXY=
set HTTPS_PROXY=
set ALL_PROXY=
set NO_PROXY=
cd /d "%~dp0"
".venv\Scripts\python.exe" -m fishbot >> fishbot.log 2>&1
