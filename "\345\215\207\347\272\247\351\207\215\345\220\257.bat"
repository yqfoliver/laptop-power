@echo off
rem Upgrade-restart launcher. ASCII ONLY in this file (codepage-safe).
rem Real work is done by relaunch.py (kill old, start resident, self-check).
cd /d "%~dp0"

set "PY=%USERPROFILE%\.workbuddy\binaries\python\versions\3.13.12\python.exe"
if exist "%PY%" goto run
rem fallback: first python on PATH
for /f "delims=" %%i in ('where python 2^>nul') do if not defined PY set "PY=%%i"

:run
"%PY%" relaunch.py
echo.
pause
