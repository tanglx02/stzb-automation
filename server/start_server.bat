@echo off
rem Start the console on Windows (bare metal). ASCII-only.
rem
rem Reads config from server\.env (copy .env.example to .env and edit it first).
rem Listens on 127.0.0.1:8000 by default - put a reverse proxy (Caddy / Nginx /
rem IIS) in front of it if you need HTTPS on a public domain.
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

echo Starting STZB console on http://127.0.0.1:8000 ...
echo Press Ctrl+C to stop.
echo.
"%PY%" -m uvicorn app.main:app --host 127.0.0.1 --port 8000
pause
