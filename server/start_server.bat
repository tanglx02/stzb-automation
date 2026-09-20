@echo off
rem Start the console on Windows (bare metal, intranet). ASCII-only.
rem
rem Reads config from server\.env (copy .env.example to .env and edit it first).
rem Binds 0.0.0.0:8000 so any device on the same LAN can open it, e.g.
rem     http://192.168.1.23:8000
rem No domain / HTTPS / reverse proxy needed for intranet use.
setlocal
cd /d "%~dp0"

set PY=python
if exist "venv\Scripts\python.exe" set PY=venv\Scripts\python.exe

"%PY%" -c "import sys" >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python not found. Run setup_server.bat first.
  pause
  exit /b 1
)

if not exist ".env" (
  echo [WARN] server\.env not found - the console will use built-in defaults
  echo        and print a random admin password / agent token on first start.
  echo        To set them yourself: copy .env.example to .env and edit it.
  echo.
)

rem --- figure out this machine's LAN address (no packet is actually sent) ---
set LANIP=
for /f "usebackq delims=" %%i in (`"%PY%" -c "import socket;s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);s.connect(('8.8.8.8',80));print(s.getsockname()[0]);s.close()" 2^>nul`) do set LANIP=%%i

echo ============================================================
echo   STZB console - starting (intranet mode)
echo ============================================================
echo.
echo   This machine : http://127.0.0.1:8000
if defined LANIP echo   Same LAN     : http://%LANIP%:8000
echo.
echo   Open the "Same LAN" address on your phone / other PC.
echo   Press Ctrl+C to stop.
echo.
"%PY%" -m uvicorn app.main:app --host 0.0.0.0 --port 8000
pause
