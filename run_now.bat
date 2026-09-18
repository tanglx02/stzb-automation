@echo off
rem Manual "run now and show me the report" launcher. ASCII-only on purpose.
rem = run_daily.py --slot auto --force --open-report (plus any extra args)
setlocal
rem Prefer the project-local venv (created by setup.bat), else python on PATH.
set PY=python
if exist "%~dp0venv\Scripts\python.exe" set PY=%~dp0venv\Scripts\python.exe
cd /d "%~dp0"

"%PY%" -c "import sys" >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python not found: %PY%
  echo Please run setup.bat first to create the environment.
  pause
  exit /b 1
)

if not exist logs mkdir logs
if not exist logs\reports mkdir logs\reports
"%PY%" run_daily.py --slot auto --force --open-report %*
echo.
echo Done. Reports are in logs\reports\ (latest.html / latest.json)
pause
