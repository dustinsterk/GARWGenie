#!/usr/bin/env bash
# Build "GARW Genie.app" on macOS (run this ON a Mac).
# Usage:  ./build.sh
set -euo pipefail
cd "$(dirname "$0")"

python3 -m venv .venv-build
source .venv-build/bin/activate
pip install --upgrade pip >/dev/null
pip install -r requirements.txt pyinstaller >/dev/null

rm -rf build dist
# App icon: build an .icns from assets/garw_genie_icon.png (macOS tools only)
ICON_ICNS="${ICON_ICNS:-}"
if [ -z "$ICON_ICNS" ] && command -v iconutil >/dev/null && [ -f assets/garw_genie_icon.png ]; then
  rm -rf build_icon.iconset && mkdir -p build_icon.iconset
  for sz in 16 32 64 128 256 512; do
    sips -z $sz $sz assets/garw_genie_icon.png --out build_icon.iconset/icon_${sz}x${sz}.png >/dev/null
    dbl=$((sz*2)); [ $dbl -le 1024 ] && sips -z $dbl $dbl assets/garw_genie_icon.png --out build_icon.iconset/icon_${sz}x${sz}@2x.png >/dev/null
  done
  iconutil -c icns build_icon.iconset -o assets/garw_genie.icns && ICON_ICNS=assets/garw_genie.icns
  rm -rf build_icon.iconset
fi
pyinstaller --noconfirm --clean \
  --name "GARW Genie" \
  --windowed \
  --onedir \
  --osx-bundle-identifier com.thesterk.garwgenie \
  --add-data "assets:assets" \
  --add-data "default_repos.txt:." \
  --add-data "TrackList.txt:." \
  --hidden-import certifi \
  --hidden-import PIL._tkinter_finder \
  --collect-all imageio_ffmpeg \
  --collect-all bleak \
  --collect-submodules lapanalysis \
  --collect-submodules pyqtgraph \
  --hidden-import PySide6.QtMultimedia \
  --hidden-import PySide6.QtMultimediaWidgets \
  --hidden-import PySide6.QtOpenGLWidgets \
  --hidden-import pyqtgraph.opengl \
  --hidden-import OpenGL.platform.darwin \
  --exclude-module PySide6.QtWebEngineCore \
  --exclude-module PySide6.QtWebEngineWidgets \
  --exclude-module PySide6.QtQuick \
  --exclude-module PySide6.QtQml \
  --exclude-module PySide6.Qt3DCore \
  --exclude-module PySide6.QtCharts \
  --exclude-module PySide6.QtDataVisualization \
  --exclude-module PySide6.QtBluetooth \
  --exclude-module PySide6.QtNfc \
  --exclude-module PySide6.QtSerialPort \
  --exclude-module PySide6.QtPositioning \
  --exclude-module PySide6.QtWebSockets \
  --exclude-module PySide6.QtDesigner \
  --exclude-module PySide6.QtHelp \
  --exclude-module PySide6.QtTest \
  --exclude-module PySide6.QtSql \
  --exclude-module matplotlib \
  --exclude-module scipy \
  --exclude-module pandas \
  --exclude-module IPython \
  ${ICON_ICNS:+--icon "$ICON_ICNS"} \
  garw_genie.py

# macOS asks the user for Bluetooth permission (the RaceBox scan) and refuses the app without this key.
# Editing Info.plist invalidates PyInstaller's ad-hoc signature, and a bundle with a broken signature
# is reported as "damaged" by Gatekeeper (fatal on Apple Silicon) — so re-sign ad-hoc afterwards.
/usr/libexec/PlistBuddy -c "Add :NSBluetoothAlwaysUsageDescription string 'GARW Genie scans for your RaceBox GPS to find its Bluetooth address.'" \
  "dist/GARW Genie.app/Contents/Info.plist" 2>/dev/null || true
codesign --force --deep --sign - "dist/GARW Genie.app"
codesign --verify --deep --strict "dist/GARW Genie.app" && echo "ad-hoc signature OK"

cp default_repos.txt dist/ 2>/dev/null || true   # editable list of default dash repos, shipped beside the app
rm -rf "dist/GARW Genie"                         # PyInstaller's raw onedir output; the .app already contains it

# Zip the .app for sharing. ditto keeps symlinks and bundle metadata intact (plain `zip -r`
# follows the Python.framework symlinks and roughly doubles the size).
( cd dist && rm -f GARW-Genie-macOS.zip \
  && ditto -c -k --sequesterRsrc --keepParent "GARW Genie.app" GARW-Genie-macOS.zip \
  && zip -q GARW-Genie-macOS.zip default_repos.txt )

echo
echo "Built: dist/GARW Genie.app   (zipped: dist/GARW-Genie-macOS.zip)"
echo
echo "NOTE: the app is only ad-hoc signed. A downloaded copy is quarantined, so on another Mac"
echo "either right-click > Open (then Open again), or allow it under System Settings > Privacy &"
echo "Security, or run:  xattr -dr com.apple.quarantine 'GARW Genie.app'"
echo "To sign/notarize:  codesign --deep --force --options runtime -s 'Developer ID Application: ...' 'dist/GARW Genie.app'"
