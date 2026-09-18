@echo off
rem Headless launcher for the STZB daily-task bot. ASCII-only content on purpose.
rem
rem Use this one when YOU trigger the run (boot script / your own scheduler).
rem Output goes to logs\console.log; the run also writes logs\run_<date>.log
rem and logs\reports\latest.html.
rem
rem Want to watch it and get the report opened automatically? Use run_now.bat.
rem
rem Usage:
rem   run_daily.bat            auto-pick the slot by current time
rem   run_daily.bat 00:00      force the 00:00 slot
rem   run_daily.bat 12:00      force the 12:00 slot
setlocal
rem Prefer the project-local venv (created by setup.bat), else python on PATH.
set PY=python
if exist "%~dp0venv\Scripts\python.exe" set PY=%~dp0venv\Scripts\python.exe
cd /d "%~dp0"

"%PY%" -c "import sys" >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python not found: %PY%
  echo Please run setup.bat first to create the environment.
  exit /b 1
)

if not exist logs mkdir logs
if not exist logs\reports mkdir logs\reports
if "%~1"=="" (
  "%PY%" run_daily.py --slot auto >> "logs\console.log" 2>&1
) else (
  "%PY%" run_daily.py --slot %1 >> "logs\console.log" 2>&1
)
exit /b %errorlevel%
