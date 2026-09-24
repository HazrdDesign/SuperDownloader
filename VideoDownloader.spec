# PyInstaller build definition for VideoDownloader.exe (one file, no console window).
# Build with scripts\build.bat, which fetches vendor\ffmpeg and vendor\deno first.
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
# importlib.metadata needs these to report the bundled engine version (see app/updater.py).
datas += copy_metadata("yt-dlp") + copy_metadata("yt-dlp-ejs")

a = Analysis(
    [str(ROOT / "app" / "main.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=["app.ui_main", "app.ui_settings", "PIL._tkinter_finder"],
    excludes=["pytest", "_pytest", "tests"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="VideoDownloader",
    icon=str(ROOT / "assets" / "icon.ico"),
    console=False,
    upx=False,  # UPX-packed executables trigger more antivirus false positives
    strip=False,
    debug=False,
)
