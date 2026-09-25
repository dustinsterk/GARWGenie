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
  --osx-bundle-identifier com.turnautomotive.garwgenie \
  --add-data "assets:assets" \
  --add-data "default_repos.txt:." \
  --hidden-import certifi \
  ${ICON_ICNS:+--icon "$ICON_ICNS"} \
  garw_genie.py

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
echo "NOTE: the app is unsigned. First launch on another Mac: right-click > Open,"
echo "or run:  xattr -dr com.apple.quarantine 'GARW Genie.app'"
echo "To sign/notarize:  codesign --deep --force --options runtime -s 'Developer ID Application: ...' 'dist/GARW Genie.app'"
