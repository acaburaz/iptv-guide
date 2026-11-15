@echo off
setlocal EnableExtensions
cd /d "%~dp0.."
if not exist "data" mkdir "data"
set "APP=%CD%\app.py"
set "LOG=%CD%\data\start.log"
set "PY="

rem A real pythonw has no console. Skip the Microsoft Store alias, which exits at once.
for /f "delims=" %%I in ('where pythonw 2^>nul') do (
  echo %%I | find /i ".exe" >nul
  if not errorlevel 1 (
    echo %%I | find /i "\WindowsApps\" >nul
    if errorlevel 1 set "PY=%%I"
  )
)
if defined PY (
  >"%LOG%" echo starting pythonw
  start "IPTV Guide" /MIN "%PY%" "%APP%" --no-browser --port 8765
  exit /b 0
)

where pyw >nul 2>&1 && pyw -3 -c "import sys" >nul 2>&1
if not errorlevel 1 (
  >"%LOG%" echo starting pyw -3
  start "IPTV Guide" /MIN pyw -3 "%APP%" --no-browser --port 8765
  exit /b 0
)

set "PY="
for /f "delims=" %%I in ('where python 2^>nul') do (
  echo %%I | find /i ".exe" >nul
  if not errorlevel 1 (
    echo %%I | find /i "\WindowsApps\" >nul
    if errorlevel 1 set "PY=%%I"
  )
)
if defined PY (
  >"%LOG%" echo starting python
  start "IPTV Guide" /MIN "%PY%" "%APP%" --no-browser --port 8765
  exit /b 0
)

where py >nul 2>&1 && py -3 -c "import sys" >nul 2>&1
if not errorlevel 1 (
  >"%LOG%" echo starting py -3
  start "IPTV Guide" /MIN py -3 "%APP%" --no-browser --port 8765
  exit /b 0
)

>"%LOG%" echo Python 3 was not found.
exit /b 1
