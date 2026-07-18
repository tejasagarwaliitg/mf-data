@echo off
cd /d "%~dp0"
title Balaji MF
if "%1"=="--install-startup" py app.py --install-startup & pause & exit
if "%1"=="--remove-startup" py app.py --remove-startup & pause & exit
echo Starting Balaji MF...
echo Browser will open at http://localhost:5050
echo Close this window to stop the server.
echo.
echo To auto-start on Windows boot, run:  start.bat --install-startup
echo.
py app.py 2>nul || python app.py
pause
