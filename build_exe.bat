@echo off
rem Builds PhotoAlbum.exe, a single self-contained executable (no Python needed to run it).
rem Requires: pip install -r requirements.txt pyinstaller
setlocal
cd /d "%~dp0"

python -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
    echo PyInstaller is not installed. Install it with:
    echo     python -m pip install pyinstaller
    exit /b 1
)

rem Intermediate files go to build\, the exe to this folder.
python -m PyInstaller --noconfirm --clean --onefile --windowed ^
    --name PhotoAlbum ^
    --distpath "%~dp0." ^
    --workpath "%~dp0build" ^
    --specpath "%~dp0build" ^
    --exclude-module tkinter ^
    main.py
if errorlevel 1 (
    echo.
    echo Build FAILED.
    exit /b 1
)

echo.
echo Built "%~dp0PhotoAlbum.exe"
endlocal
