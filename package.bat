@echo off
setlocal
cd /d "%~dp0"
title CheckinHub Packager

set OUT=deploy\checkin-hub.tar.gz
if exist "%OUT%" del /q "%OUT%"

echo Packaging project for Linux server...
echo.

if /i "%~1"=="--no-data" goto nodata
if not exist "data\checkin.db" goto nodata

echo   Including: data\  (credentials encrypted with data\master.key)
echo   Keep this package private - it can log in as you.
tar -czf "%OUT%" --exclude=.venv --exclude=__pycache__ --exclude=.git --exclude=*.pyc --exclude=.env --exclude=wb.json --exclude=qoder.json --exclude=.t --exclude=.t2 --exclude=data/logs --exclude=deploy/checkin-hub.tar.gz .
goto done

:nodata
echo   Excluding: data\  (server will start with an empty database)
tar -czf "%OUT%" --exclude=.venv --exclude=__pycache__ --exclude=.git --exclude=*.pyc --exclude=.env --exclude=wb.json --exclude=qoder.json --exclude=data --exclude=deploy/checkin-hub.tar.gz .
goto done

:done
if not exist "%OUT%" goto fail
echo.
echo Done: %OUT%
echo.
echo Next:
echo   1) scp deploy\checkin-hub.tar.gz root@YOUR_SERVER_IP:/root/
echo   2) ssh root@YOUR_SERVER_IP
echo   3) mkdir -p /opt/checkin-hub ^&^& tar -xzf /root/checkin-hub.tar.gz -C /opt/checkin-hub
echo   4) bash /opt/checkin-hub/deploy/deploy.sh
echo.
pause
goto end

:fail
echo.
echo Packaging failed. tar.exe is required (built into Windows 10 1803+).
pause

:end
endlocal
