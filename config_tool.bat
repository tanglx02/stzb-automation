@echo off
rem Interactive config tool for the local client. ASCII-only on purpose.
rem
rem This tool is OPTIONAL. It only reads/writes config.json in this folder.
rem Not using it changes nothing - the bot runs fine on the built-in defaults.
rem
rem IMPORTANT: use `cd /d "%~dp0"` (stay in THIS folder = the project root).
rem `%~dp0` already ends with a backslash, so "%~dp0.." climbs UP one level
rem to E:\Project\ and python can no longer find tools\config.py. Fixed once,
rem keep an eye on it - selftest has a guard for this pattern.
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
"%PY%" tools\config.py %*
echo.
pause
