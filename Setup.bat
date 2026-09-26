@echo off
cd /d "%~dp0"
python -m venv .venv
if errorlevel 1 goto failed
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto failed
echo Setup finished. Double-click Start Demo.bat to start the application.
pause
exit /b 0
:failed
echo Setup failed. Check that Python is installed and your internet connection is available.
pause
exit /b 1
