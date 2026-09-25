# PyInstaller build definition for Super Downloader.
#
# Windows (scripts\build.bat, which fetches vendor\ffmpeg and vendor\deno first) produces two
# things from one analysis:
#   dist\SuperDownloader-Portable.exe portable single file (unpacks itself on every start)
#   dist\SuperDownloader-folder\      the same app as a folder: starts much faster; the installer
#                                     (installer\SuperDownloader.iss) packages this folder
# Mac (scripts/build_mac.sh, which runs scripts/fetch_tools_mac.sh first) produces:
#   dist/Super Downloader.app         for Apple silicon; build_mac.sh puts it in a .dmg
# -*- mode: python ; coding: utf-8 -*-

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, copy_metadata

ROOT = Path(SPECPATH)
VENDOR = ROOT / "vendor"
MAC = sys.platform == "darwin"
EXE_SUFFIX = "" if MAC else ".exe"
FETCH = "scripts/fetch_tools_mac.sh" if MAC else "scripts\\fetch_ffmpeg.ps1"

tools = [(VENDOR / "ffmpeg" / f"ffmpeg{EXE_SUFFIX}", "ffmpeg"),
         (VENDOR / "ffmpeg" / f"ffprobe{EXE_SUFFIX}", "ffmpeg"),
         (VENDOR / "deno" / f"deno{EXE_SUFFIX}", "deno")]
for required, _ in tools:
    if not required.is_file():
        raise SystemExit(f"Missing {required}. Run {FETCH} first.")

binaries = [(str(path), folder) for path, folder in tools]

datas = [
    (str(ROOT / "assets" / "icon.ico"), "assets"),
    (str(ROOT / "assets" / "icon.icns"), "assets"),
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
    icon=str(ROOT / "assets" / ("icon.icns" if MAC else "icon.ico")),
    console=False,
    upx=False,  # UPX-packed executables trigger more antivirus false positives
    strip=False,
    debug=False,
)

if MAC:
    sys.path.insert(0, str(ROOT))
    from app import __version__

    mac_exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="SuperDownloader", target_arch="arm64",
                  argv_emulation=False, **common)
    mac_folder = COLLECT(mac_exe, a.binaries, a.datas, name="SuperDownloader-mac", strip=False, upx=False)
    BUNDLE(
        mac_folder,
        name="Super Downloader.app",
        icon=common["icon"],
        bundle_identifier="co.dgnl.superdownloader",
        version=__version__,
        info_plist={
            "CFBundleDisplayName": "Super Downloader",
            "CFBundleShortVersionString": __version__,
            "CFBundleVersion": __version__,
            "LSMinimumSystemVersion": "11.0",
            "NSHighResolutionCapable": True,
            "LSApplicationCategoryType": "public.app-category.utilities",
        },
    )
else:
    # Portable single file.
    portable = EXE(pyz, a.scripts, a.binaries, a.datas, [], name="SuperDownloader-Portable", **common)

    # Folder version (fast start) for the installer.
    folder_exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="SuperDownloader", **common)
    COLLECT(folder_exe, a.binaries, a.datas, name="SuperDownloader-folder", strip=False, upx=False)
