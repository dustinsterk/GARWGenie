@echo off
REM Build "GARW Genie.exe" on Windows (run this ON Windows).
REM Usage:  build.bat
setlocal
cd /d "%~dp0"

python -m venv .venv-build || goto :fail
call .venv-build\Scripts\activate.bat
python -m pip install --upgrade pip >nul
pip install -r requirements.txt pyinstaller >nul || goto :fail

if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

set ICON_ARG=
if exist assets\garw_genie.ico set ICON_ARG=--icon assets\garw_genie.ico

pyinstaller --noconfirm --clean ^
  --name "GARW Genie" ^
  --windowed ^
  --onefile ^
  --add-data "assets;assets" ^
  --add-data "default_repos.txt;." ^
  --add-data "TrackList.txt;." ^
  --hidden-import certifi ^
  --hidden-import PIL._tkinter_finder ^
  --collect-all imageio_ffmpeg ^
  --collect-all bleak ^
  --collect-submodules lapanalysis ^
  --collect-submodules pyqtgraph ^
  --hidden-import PySide6.QtMultimedia ^
  --hidden-import PySide6.QtMultimediaWidgets ^
  --hidden-import PySide6.QtOpenGLWidgets ^
  --hidden-import pyqtgraph.opengl ^
  --hidden-import OpenGL.platform.win32 ^
  --exclude-module PySide6.QtWebEngineCore ^
  --exclude-module PySide6.QtWebEngineWidgets ^
  --exclude-module PySide6.QtQuick ^
  --exclude-module PySide6.QtQml ^
  --exclude-module PySide6.Qt3DCore ^
  --exclude-module PySide6.QtCharts ^
  --exclude-module PySide6.QtDataVisualization ^
  --exclude-module PySide6.QtBluetooth ^
  --exclude-module PySide6.QtNfc ^
  --exclude-module PySide6.QtSerialPort ^
  --exclude-module PySide6.QtPositioning ^
  --exclude-module PySide6.QtWebSockets ^
  --exclude-module PySide6.QtDesigner ^
  --exclude-module PySide6.QtHelp ^
  --exclude-module PySide6.QtTest ^
  --exclude-module PySide6.QtSql ^
  --exclude-module matplotlib ^
  --exclude-module scipy ^
  --exclude-module pandas ^
  --exclude-module IPython ^
  %ICON_ARG% ^
  garw_genie.py || goto :fail

echo.
copy /Y default_repos.txt dist\ >nul
echo Built: dist\GARW Genie.exe  (+ default_repos.txt beside it)
echo.
echo NOTE: the exe is unsigned, so Windows SmartScreen will show "unrecognized app"
echo on first run (More info ^> Run anyway). Code-signing removes that.
exit /b 0

:fail
echo Build failed.
exit /b 1
