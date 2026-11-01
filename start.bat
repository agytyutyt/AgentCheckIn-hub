@echo off
setlocal
cd /d "%~dp0"
title CheckinHub

if exist ".venv\Scripts\python.exe" goto have_venv

echo [1/4] First run: creating Python environment (about 1 minute)...
python -m venv .venv
if errorlevel 1 py -3 -m venv .venv
if errorlevel 1 goto fail
".venv\Scripts\python.exe" -m pip install -q --upgrade pip
".venv\Scripts\python.exe" -m pip install -q -r requirements.txt
if errorlevel 1 goto fail
goto check_env

:have_venv
echo [1/4] Python environment ready.

:check_env
if exist ".env" goto env_ready
copy /y ".env.example" ".env" >nul
echo [2/4] Created .env with defaults.
goto import

:env_ready
echo [2/4] Configuration ready.

:import
echo [3/4] Checking accounts from desktop clients...
".venv\Scripts\python.exe" tools\import_all.py
echo.

:run
netstat -ano | findstr ":8000" | findstr "LISTENING" >nul
if not errorlevel 1 goto already_running

echo [4/4] Starting panel...
echo.
echo        URL   : http://127.0.0.1:8000
echo        Login : admin / admin123
echo        Stop  : Ctrl+C (keep this window open)
echo.
start "" powershell -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 4; Start-Process 'http://127.0.0.1:8000'"
".venv\Scripts\python.exe" -m app.serve
if errorlevel 1 goto fail
goto end

:fail
echo.
echo Startup failed.
echo Make sure Python 3.10+ is installed and "python" is available on PATH.
echo If the environment is broken, delete the .venv folder and run start.bat again.
pause
goto end

:already_running
echo [4/4] Panel is already running on port 8000.
echo       Opening browser... (switch to the existing window to stop it)
start "" powershell -NoProfile -WindowStyle Hidden -Command "Start-Process 'http://127.0.0.1:8000'"
ping -n 4 127.0.0.1 >nul

:end
endlocal
