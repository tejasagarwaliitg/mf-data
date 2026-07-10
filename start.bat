@echo off
cd /d "%~dp0"
echo Starting Balaji MF...
start http://localhost:5050
python app.py
pause
