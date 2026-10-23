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
rem start /min gives python its own console: if some other console gets a
rem CTRL_C/close event, it no longer takes the bot down with it
rem (note: the redirection must live INSIDE the inner cmd, otherwise the
rem  child console swallows it and fishbot.log stays empty)
start "fishbot" /min cmd /c "".venv\Scripts\python.exe" -m fishbot 1>>fishbot.log 2>&1"
