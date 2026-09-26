@echo off
cd /d "%~dp0"
echo Open http://127.0.0.1:5000 in your browser after the server starts.
echo Keep this window open while using the application. Press Ctrl+C to stop.
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" app.py --demo
) else (
  python app.py --demo
)
pause
