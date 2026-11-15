@echo off
setlocal
cd /d "%~dp0.."
set "APP=%~dp0..\app.py"
where pythonw >nul 2>&1
if not errorlevel 1 (
  start "IPTV Guide" /MIN pythonw "%APP%" --no-browser --port 8765
  exit /b 0
)
where python >nul 2>&1
if not errorlevel 1 (
  start "IPTV Guide" /MIN python "%APP%" --no-browser --port 8765
  exit /b 0
)
where py >nul 2>&1
if not errorlevel 1 (
  start "IPTV Guide" /MIN py -3 "%APP%" --no-browser --port 8765
  exit /b 0
)
echo Python 3 was not found.>&2
exit /b 1
