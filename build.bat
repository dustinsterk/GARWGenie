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
