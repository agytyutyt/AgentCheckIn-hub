@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" goto novenv

echo [1/2] WorkBuddy ...
".venv\Scripts\python.exe" tools\import_workbuddy.py --save
echo.
echo [2/2] Qoder ...
".venv\Scripts\python.exe" tools\import_qoder.py --save
echo.
echo Done. Open http://127.0.0.1:8000 and refresh the page.
echo.
pause
goto end

:novenv
echo Python environment not found.
echo Please double-click start.bat once, then run this again.
pause

:end
endlocal
