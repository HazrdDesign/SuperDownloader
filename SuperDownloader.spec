# PyInstaller build definition for Super Downloader. Build with scripts\build.bat, which fetches
# vendor\ffmpeg and vendor\deno first. Produces two things from one analysis:
#   dist\SuperDownloader-Portable.exe portable single file (unpacks itself on every start)
#   dist\SuperDownloader-folder\      the same app as a folder: starts much faster; the installer
#                                     (installer\SuperDownloader.iss) packages this folder
# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, copy_metadata

ROOT = Path(SPECPATH)
VENDOR = ROOT / "vendor"

for required in (VENDOR / "ffmpeg" / "ffmpeg.exe", VENDOR / "ffmpeg" / "ffprobe.exe", VENDOR / "deno" / "deno.exe"):
    if not required.is_file():
        raise SystemExit(f"Missing {required}. Run scripts\\fetch_ffmpeg.ps1 first.")

binaries = [
    (str(VENDOR / "ffmpeg" / "ffmpeg.exe"), "ffmpeg"),
    (str(VENDOR / "ffmpeg" / "ffprobe.exe"), "ffmpeg"),
    (str(VENDOR / "deno" / "deno.exe"), "deno"),
]

datas = [
    (str(ROOT / "assets" / "icon.ico"), "assets"),
    (str(ROOT / "assets" / "icon.png"), "assets"),
    (str(VENDOR / "ffmpeg" / "LICENSE"), "ffmpeg"),
    (str(VENDOR / "ffmpeg" / "VERSION.txt"), "ffmpeg"),
    (str(VENDOR / "deno" / "VERSION.txt"), "deno"),
]
datas += collect_data_files("customtkinter")
datas += collect_data_files("tkinterdnd2")  # the tkdnd drag-and-drop library for Tk
# importlib.metadata needs these to report the bundled engine version (see app/updater.py).
datas += copy_metadata("yt-dlp") + copy_metadata("yt-dlp-ejs")

a = Analysis(
    [str(ROOT / "app" / "main.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=["app.ui_main", "app.ui_settings", "PIL._tkinter_finder", "tkinterdnd2"],
    excludes=["pytest", "_pytest", "tests"],
    noarchive=False,
)
pyz = PYZ(a.pure)

common = dict(
    icon=str(ROOT / "assets" / "icon.ico"),
    console=False,
    upx=False,  # UPX-packed executables trigger more antivirus false positives
    strip=False,
    debug=False,
)

# Portable single file.
portable = EXE(pyz, a.scripts, a.binaries, a.datas, [], name="SuperDownloader-Portable", **common)

# Folder version (fast start) for the installer.
folder_exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="SuperDownloader", **common)
COLLECT(folder_exe, a.binaries, a.datas, name="SuperDownloader-folder", strip=False, upx=False)
