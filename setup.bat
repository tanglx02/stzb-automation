@echo off
rem One-time setup for a fresh clone. ASCII-only on purpose
rem (Chinese text in a .bat runs under a GBK console and shows mojibake).
rem
rem Creates a local venv and installs the client dependencies, so the .bat
rem launchers can find a known-good Python instead of guessing from PATH.
rem
rem After this finishes, use run_now.bat / config_tool.bat / run_daily.bat.
setlocal
cd /d "%~dp0"

echo ============================================================
echo   STZB automation - first-time environment setup
echo ============================================================
echo.

rem 1) find a working Python: prefer the py launcher, then python on PATH
set PY=python
where py >nul 2>nul && set PY=py -3

rem 2) create venv (skip if it already exists)
if exist "venv\Scripts\python.exe" (
  echo [skip] venv already exists
) else (
  echo [1/2] creating venv with: %PY%
  %PY% -m venv venv
  if errorlevel 1 (
    echo.
    echo !! failed to create venv.
    echo    Please install Python 3.11+ and tick "Add to PATH", then rerun.
    pause
    exit /b 1
  )
)

rem 3) install dependencies
echo [2/2] installing client dependencies ^(may take a few minutes^)...
"venv\Scripts\python.exe" -m pip install --upgrade pip >nul
"venv\Scripts\python.exe" -m pip install -r requirements-client.txt
if errorlevel 1 (
  echo.
  echo !! dependency install failed. Check your network and rerun.
  pause
  exit /b 1
)

echo.
echo ============================================================
echo   Done. Environment is ready.
echo   Next:
echo     - configure MuMu paths etc: double-click config_tool.bat
echo     - try one run:              double-click run_now.bat
echo ============================================================
echo.
pause
