@echo off
cd /d "%~dp0"
echo ========================================
echo  Balaji MF - Windows Setup
echo ========================================
echo.
echo Step 1: Checking Python...
py --version >nul 2>&1
if %errorlevel% neq 0 (
    python --version >nul 2>&1
    if %errorlevel% neq 0 (
        echo ERROR: Python not found!
        echo Download Python from https://python.org
        echo Make sure to check "Add Python to PATH"
        pause
        exit /b 1
    )
)
echo Python found.
echo.
echo Step 2: Installing dependencies...
echo (This takes 30-60 seconds, please wait...)
echo.
py -m pip install -r requirements.txt
if %errorlevel% neq 0 (
    python -m pip install -r requirements.txt
)
echo.
echo ========================================
echo  SETUP COMPLETE!
echo ========================================
echo.
echo Starting app...
start "" "%~dp0start.bat"
timeout /t 3 >nul
exit
