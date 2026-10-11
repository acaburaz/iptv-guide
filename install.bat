@echo off
setlocal EnableExtensions
cd /d "%~dp0"
where python >nul 2>&1
if errorlevel 1 (
  echo Python 3 is required.
  echo Install it from https://www.python.org/downloads/ and run this again.
  pause
  exit /b 1
)
if not defined APPDATA (
  echo APPDATA is not set, so VLC's extension folder cannot be found.
  pause
  exit /b 1
)
set "IPTV_ROOT=%CD%"
python -c "import os, pathlib; root = pathlib.Path(os.environ['IPTV_ROOT']); text = (root / 'vlc' / 'iptv-rack.lua').read_text(encoding='utf-8').replace('__IPTV_RACK_DIR__', str(root)); dest = pathlib.Path(os.environ['APPDATA']) / 'vlc' / 'lua' / 'extensions' / 'iptv-rack.lua'; dest.parent.mkdir(parents=True, exist_ok=True); dest.write_text(text, encoding='utf-8', newline='\n'); print('Installed', dest)"
if errorlevel 1 (
  pause
  exit /b 1
)
echo.
echo Quit VLC completely, open it again, then choose View -^> IPTV Guide.
pause
