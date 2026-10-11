@echo off
cd /d "%~dp0"
where python >nul 2>&1
if errorlevel 1 (
  echo Python 3 is required.
  pause
  exit /b 1
)
python app.py
if errorlevel 1 pause
