@echo off
setlocal EnableExtensions
cd /d "%~dp0.."
if not exist "data" mkdir "data"
set "APP=%CD%\app.py"
set "LOG=%CD%\data\start.log"
set "PY="
set "ARGS="
>"%LOG%" echo looking for Python 3

call :from_saved
if defined PY goto :run
call :from_localapp
if defined PY goto :run
call :from_program_files
if defined PY goto :run
call :from_registry
if defined PY goto :run
call :from_where
if defined PY goto :run
call :from_py_launcher
if defined PY goto :run

>>"%LOG%" echo Python 3 was not found.
exit /b 1

:run
>>"%LOG%" echo starting %PY%
start "IPTV Guide" /MIN "%PY%" %ARGS% "%APP%" --no-browser --port 8765
exit /b 0

:from_saved
if not exist "%CD%\data\python.txt" exit /b 0
set /p SAVED=<"%CD%\data\python.txt"
call :accept "%SAVED%"
exit /b 0

:from_localapp
if not defined LOCALAPPDATA exit /b 0
for /f "delims=" %%I in ('dir /b /ad /o-d "%LOCALAPPDATA%\Programs\Python\Python3*" 2^>nul') do (
  if not defined PY call :accept "%LOCALAPPDATA%\Programs\Python\%%I\pythonw.exe"
)
exit /b 0

:from_program_files
for %%R in ("%ProgramFiles%" "%ProgramFiles(x86)%") do (
  if not defined PY for /f "delims=" %%I in ('dir /b /ad /o-d "%%~R\Python3*" 2^>nul') do (
    if not defined PY call :accept "%%~R\%%I\pythonw.exe"
  )
)
exit /b 0

:from_registry
for %%K in ("HKCU\Software\Python\PythonCore" "HKLM\Software\Python\PythonCore" "HKLM\Software\WOW6432Node\Python\PythonCore") do (
  if not defined PY for /f "tokens=1,2,*" %%A in ('reg query %%~K /s /v ExecutablePath 2^>nul ^| find "REG_SZ"') do (
    if not defined PY call :accept "%%C"
  )
)
exit /b 0

:from_where
for %%N in (pythonw.exe python.exe python3.exe) do (
  if not defined PY for /f "delims=" %%I in ('where %%N 2^>nul') do (
    if not defined PY call :accept "%%I"
  )
)
exit /b 0

:from_py_launcher
if exist "%SystemRoot%\pyw.exe" (
  "%SystemRoot%\pyw.exe" -3 -c "import sys" >nul 2>&1
  if not errorlevel 1 (
    set "PY=%SystemRoot%\pyw.exe"
    set "ARGS=-3"
    exit /b 0
  )
)
if exist "%SystemRoot%\py.exe" (
  "%SystemRoot%\py.exe" -3 -c "import sys" >nul 2>&1
  if not errorlevel 1 (
    set "PY=%SystemRoot%\py.exe"
    set "ARGS=-3"
  )
)
exit /b 0

:accept
if "%~1"=="" exit /b 0
if not exist "%~1" exit /b 0
echo "%~1"| find /i "\WindowsApps\" >nul && exit /b 0
if %~z1 EQU 0 exit /b 0
"%~1" -c "import sys" >nul 2>&1 || exit /b 0
set "PY=%~1"
if /i "%~nx1"=="python.exe" if exist "%~dp1pythonw.exe" (
  echo "%~dp1pythonw.exe"| find /i "\WindowsApps\" >nul || set "PY=%~dp1pythonw.exe"
)
exit /b 0
