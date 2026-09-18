@echo off
rem One-time setup for running the console on Windows (bare metal). ASCII-only.
rem
rem Creates a local venv and installs the server dependencies.
setlocal
cd /d "%~dp0"

echo ============================================================
echo   STZB console - Windows setup
echo ============================================================
echo.

set PY=python
where py >nul 2>nul && set PY=py -3

if exist "venv\Scripts\python.exe" (
  echo [skip] venv already exists
) else (
  echo [1/2] creating venv with: %PY%
  %PY% -m venv venv
  if errorlevel 1 (
    echo !! failed to create venv. Install Python 3.11+ and rerun.
    pause
    exit /b 1
  )
)

echo [2/2] installing server dependencies...
"venv\Scripts\python.exe" -m pip install --upgrade pip >nul
"venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
  echo !! dependency install failed. Check network and rerun.
  pause
  exit /b 1
)

echo.
echo Done. Next: copy .env.example to .env and edit it, then run start_server.bat
echo.
pause
