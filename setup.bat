@echo off
cd /d "%~dp0"
echo ========================================
echo  Balaji MF - Windows Setup
echo ========================================
echo.
echo Installing dependencies (one-time)...
pip install -q -r requirements.txt
if %errorlevel% neq 0 (
    echo.
    echo ERROR: pip install failed.
    echo Make sure Python is installed from python.org
    echo (check "Add Python to PATH" during installation)
    echo.
    pause
    exit /b 1
)
echo.
echo SUCCESS! You can now use start.vbs to launch the app.
echo A browser will open at http://localhost:5050
echo.
pause
