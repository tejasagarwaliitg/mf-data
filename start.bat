@echo off
cd /d "%~dp0"
title Balaji MF
if "%1"=="--install-startup" py app.py --install-startup & pause & exit
if "%1"=="--remove-startup" py app.py --remove-startup & pause & exit
where py >nul 2>nul
if %errorlevel% neq 0 (
    where python >nul 2>nul
    if %errorlevel% neq 0 (
        echo ERROR: Python not found!
        echo Install Python from https://python.org
        echo Check "Add Python to PATH" during install.
        pause
        exit /b 1
    )
    set PYCMD=python
) else (
    set PYCMD=py
)
%PYCMD% -c "import numpy" 2>nul
if %errorlevel% neq 0 (
    echo Installing dependencies (one-time)...
    %PYCMD% -m pip install -r requirements.txt
    if %errorlevel% neq 0 (
        echo ERROR: pip install failed.
        echo Check your internet connection.
        pause
        exit /b 1
    )
)
echo Starting Balaji MF...
echo Browser will open at http://localhost:5050
echo Close this window to stop the server.
echo.
echo To auto-start on Windows boot, run:  start.bat --install-startup
echo.
%PYCMD% app.py
echo.
echo App stopped unexpectedly. Check errors above.
pause
