@echo off
cd /d "%~dp0"
title Balaji MF
echo Starting Balaji MF...
git pull >nul 2>nul
echo Browser will open at http://localhost:5050
echo Close this window to stop the server.
echo.
py app.py 2>nul || python app.py
pause
