@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem ============================================================
rem  笔记本电源自适应 —— 启动器
rem    双击        = 静默常驻（托盘 + 本地面板）
rem    启动.bat 面板 = 常驻并直接打开控制面板
rem    启动.bat 状态 = 只看一眼当前电源状态，不常驻
rem ============================================================

rem —— 依次找一个能用的 Python，优先无窗口版 ——
set "PYW="
set "PY="

if exist "%~dp0_runtime\python\pythonw.exe" set "PYW=%~dp0_runtime\python\pythonw.exe"
if exist "%~dp0_runtime\python\python.exe"  set "PY=%~dp0_runtime\python\python.exe"

if not defined PYW (
  if exist "%USERPROFILE%\.workbuddy\binaries\python\versions\3.13.12\pythonw.exe" (
    set "PYW=%USERPROFILE%\.workbuddy\binaries\python\versions\3.13.12\pythonw.exe"
  )
)
if not defined PY (
  if exist "%USERPROFILE%\.workbuddy\binaries\python\versions\3.13.12\python.exe" (
    set "PY=%USERPROFILE%\.workbuddy\binaries\python\versions\3.13.12\python.exe"
  )
)

if not defined PY (
  where python >nul 2>nul
  if %errorlevel%==0 for /f "delims=" %%i in ('where python') do if not defined PY set "PY=%%i"
)

if not defined PYW if defined PY (
  if exist "%PY%w.exe" set "PYW=%PY%w.exe"
)

if not defined PY (
  echo 没有找到 Python，无法启动。
  echo 请在本文件开头把 PY= 改成你机器上的 python.exe 路径。
  pause
  exit /b 1
)

if /i "%~1"=="状态" (
  "%PY%" main.py --status
  pause
  exit /b
)

if defined PYW (
  start "" "%PYW%" main.py
) else (
  start "" /min "%PY%" main.py
)

if /i "%~1"=="面板" (
  rem 唤起常驻实例的原生桌面面板（已有实例就只开窗，不重复启动）
  if defined PYW (
    "%PYW%" main.py --panel
  ) else (
    "%PY%" main.py --panel
  )
)
exit /b
