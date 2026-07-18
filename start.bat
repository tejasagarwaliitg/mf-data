@echo off
cd /d "%~dp0"
title Balaji MF
if "%1"=="--install-startup" py app.py --install-startup & pause & exit
if "%1"=="--remove-startup" py app.py --remove-startup & pause & exit
py -c "import numpy" 2>nul
if %errorlevel% neq 0 (
    echo Installing dependencies (one-time)...
    py -m pip install -r requirements.txt
)
echo Starting Balaji MF...
echo Browser will open at http://localhost:5050
echo Close this window to stop the server.
echo.
echo To auto-start on Windows boot, run:  start.bat --install-startup
echo.
py app.py
pause
