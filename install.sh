#!/usr/bin/env bash
# Install the IPTV Guide extension into VLC on Windows.
# From Git Bash this finds the same folder as install.bat.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

if command -v python >/dev/null 2>&1; then
  PYTHON=python
elif command -v python3 >/dev/null 2>&1; then
  PYTHON=python3
else
  echo "Python 3 is required."
  echo "Install it from https://www.python.org/downloads/ and run this again."
  exit 1
fi

if [[ -z "${APPDATA:-}" ]]; then
  echo "This installs the plugin into VLC on Windows."
  echo "Run install.bat, or run this script from Git Bash."
  exit 1
fi

if command -v cygpath >/dev/null 2>&1; then
  export IPTV_ROOT="$(cygpath -w "$(pwd)")"
else
  export IPTV_ROOT="$(pwd)"
fi

"$PYTHON" -c "import os, pathlib; root = pathlib.Path(os.environ['IPTV_ROOT']); text = (root / 'vlc' / 'iptv-rack.lua').read_text(encoding='utf-8').replace('__IPTV_RACK_DIR__', str(root)); dest = pathlib.Path(os.environ['APPDATA']) / 'vlc' / 'lua' / 'extensions' / 'iptv-rack.lua'; dest.parent.mkdir(parents=True, exist_ok=True); dest.write_text(text, encoding='utf-8', newline='\n'); print('Installed', dest)"

echo
echo "Quit VLC completely, open it again, then choose View -> IPTV Guide."
