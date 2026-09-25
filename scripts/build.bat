@echo off
rem Builds Super Downloader from a clean checkout. Double-click or run from a prompt.
rem Steps: find Python 3.11+ -> venv -> pip install -> fetch FFmpeg/Deno -> pytest -> PyInstaller -> self-test
setlocal EnableExtensions
cd /d "%~dp0.."

echo.
echo === Super Downloader build ===
echo.

rem ---- 1. Find Python 3.11 or newer -----------------------------------------------------------
set "PY="
for %%V in (3.13 3.12 3.11) do (
    if not defined PY (
        py -%%V -c "import sys" >nul 2>&1 && set "PY=py -%%V"
    )
)
if not defined PY (
    python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1 && set "PY=python"
)
if not defined PY (
    echo Python 3.11 or newer was not found. Install it from https://www.python.org/downloads/
    echo and tick "Add python.exe to PATH" during setup, then run this again.
    goto :fail
)
echo Using %PY%
%PY% --version

rem ---- 2. Virtual environment --------------------------------------------------------------------
if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment in .venv ...
    %PY% -m venv .venv || goto :fail
)
set "VPY=%CD%\.venv\Scripts\python.exe"

rem ---- 3. Dependencies ---------------------------------------------------------------------------
echo Installing dependencies ...
"%VPY%" -m pip install --disable-pip-version-check --upgrade pip >nul || goto :fail
"%VPY%" -m pip install --disable-pip-version-check -r requirements.txt || goto :fail

rem ---- 4. FFmpeg + Deno (pinned, checksum-verified) ------------------------------------------------
echo Fetching FFmpeg and Deno ...
powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\fetch_ffmpeg.ps1" || goto :fail

rem ---- 5. Tests ----------------------------------------------------------------------------------
echo Running tests ...
"%VPY%" -m pytest || goto :fail

rem ---- 6. Package --------------------------------------------------------------------------------
echo Building the app (this takes a few minutes) ...
"%VPY%" -m PyInstaller --noconfirm --clean SuperDownloader.spec || goto :fail

rem ---- 7. Self-test both builds (headless: engine, FFmpeg, Deno, offline download, windows) -------
powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\selftest.ps1" || (
    echo The portable exe failed its self-test.
    goto :fail
)
powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\selftest.ps1" -Exe "dist\SuperDownloader-folder\SuperDownloader.exe" -Report "build\selftest-folder.json" || (
    echo The installer build failed its self-test.
    goto :fail
)

rem ---- 8. Installer (only if Inno Setup 6 is installed) -------------------------------------------
set "ISCC="
for %%P in ("%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" "%ProgramFiles%\Inno Setup 6\ISCC.exe" "%LocalAppData%\Programs\Inno Setup 6\ISCC.exe") do (
    if not defined ISCC if exist "%%~P" set "ISCC=%%~P"
)
if not defined ISCC for /f "delims=" %%P in ('where iscc 2^>nul') do if not defined ISCC set "ISCC=%%P"
if not defined ISCC goto :no_installer
echo Building the installer ...
"%VPY%" -c "import app; print(app.__version__)" > "build\version.txt" || goto :fail
set /p APPVER=<"build\version.txt"
"%ISCC%" /Q /DAppVersion=%APPVER% "installer\SuperDownloader.iss" || goto :fail
goto :installer_done
:no_installer
echo Inno Setup 6 was not found, so no installer was built. Get it from https://jrsoftware.org/isinfo.php
echo to also build dist\SuperDownloader-Setup.exe. The portable exe works without it.
:installer_done

echo.
echo Done:
echo   %CD%\dist\SuperDownloader-Portable.exe   (single file, runs anywhere)
if defined ISCC echo   %CD%\dist\SuperDownloader-Setup.exe      (installer: faster start, Start menu shortcut)
echo.
if not defined CI pause
exit /b 0

:fail
)
echo Using %PY%
%PY% --version

rem ---- 2. Virtual environment --------------------------------------------------------------------
if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment in .venv ...
    %PY% -m venv .venv || goto :fail
)
set "VPY=%CD%\.venv\Scripts\python.exe"

rem ---- 3. Dependencies ---------------------------------------------------------------------------
echo Installing dependencies ...
"%VPY%" -m pip install --disable-pip-version-check --upgrade pip >nul || goto :fail
"%VPY%" -m pip install --disable-pip-version-check -r requirements.txt || goto :fail

rem ---- 4. FFmpeg + Deno (pinned, checksum-verified) ------------------------------------------------
echo Fetching FFmpeg and Deno ...
powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\fetch_ffmpeg.ps1" || goto :fail

rem ---- 5. Tests ----------------------------------------------------------------------------------
echo Running tests ...
"%VPY%" -m pytest || goto :fail

rem ---- 6. Package --------------------------------------------------------------------------------
echo Building the exe (this takes a few minutes) ...
"%VPY%" -m PyInstaller --noconfirm --clean SuperDownloader.spec || goto :fail

rem ---- 7. Self-test the exe (headless: engine, FFmpeg, Deno, offline download, windows) -----------
powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\selftest.ps1" || (
    echo The built exe failed its self-test.
    goto :fail
)

echo.
echo Done: %CD%\dist\SuperDownloader.exe
echo.
if not defined CI pause
exit /b 0

:fail
echo.
echo BUILD FAILED. See the messages above.
echo.
if not defined CI pause
exit /b 1
