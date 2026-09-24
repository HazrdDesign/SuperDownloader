"""Well-known locations: app data folder, the user's Downloads folder, bundled binaries."""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "VideoDownloader"

# FOLDERID_Downloads, see https://learn.microsoft.com/windows/win32/shell/knownfolderid
_FOLDERID_DOWNLOADS = "{374DE290-123F-4565-9164-39C4925E467B}"


def app_data_dir() -> Path:
    """``%APPDATA%\\VideoDownloader`` on Windows; an XDG-style folder elsewhere.

    ``VD_APPDATA`` overrides the location (used by tests and CI).
    """
    override = os.environ.get("VD_APPDATA")
    if override:
        base = Path(override)
    elif sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / APP_NAME
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / APP_NAME
    return base


def settings_file() -> Path:
    return app_data_dir() / "settings.json"


def logs_dir() -> Path:
    return app_data_dir() / "logs"


def engine_dir() -> Path:
    return app_data_dir() / "engine"


def downloads_dir() -> Path:
    """The user's Downloads folder, resolved through the Known Folder API on Windows.

    Handles users who moved Downloads to another drive or into OneDrive.
    """
    if sys.platform == "win32":
        found = _known_folder_path(_FOLDERID_DOWNLOADS)
        if found:
            return Path(found)
    return Path.home() / "Downloads"


def _known_folder_path(folder_id: str) -> str | None:
    try:
        import ctypes
        from ctypes import wintypes
        import uuid

        class GUID(ctypes.Structure):
            _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                        ("Data3", wintypes.WORD), ("Data4", wintypes.BYTE * 8)]

        u = uuid.UUID(folder_id)
        guid = GUID(u.fields[0], u.fields[1], u.fields[2],
                    (wintypes.BYTE * 8).from_buffer_copy(u.bytes[8:]))
        path_ptr = ctypes.c_wchar_p()
        shell32 = ctypes.windll.shell32  # type: ignore[attr-defined]
        shell32.SHGetKnownFolderPath.argtypes = [
            ctypes.POINTER(GUID), wintypes.DWORD, wintypes.HANDLE, ctypes.POINTER(ctypes.c_wchar_p)]
        hr = shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(path_ptr))
        try:
            if hr != 0:
                return None
            return path_ptr.value
        finally:
            ctypes.windll.ole32.CoTaskMemFree(path_ptr)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - any failure falls back to ~/Downloads
        return None


def bundle_dir() -> Path:
    """Folder that holds bundled resources: PyInstaller's unpack dir, or the project root."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)  # type: ignore[attr-defined]
    return Path(__file__).resolve().parent.parent


def asset(name: str) -> Path:
    return bundle_dir() / "assets" / name


def _bundled_exe(subdir: str, name: str) -> Path | None:
    exe = name + (".exe" if sys.platform == "win32" else "")
    for candidate in (bundle_dir() / subdir / exe, bundle_dir() / "vendor" / subdir / exe):
        if candidate.is_file():
            return candidate
    return None


def ffmpeg_dir() -> Path | None:
    """Folder containing the bundled ffmpeg/ffprobe, or None to let yt-dlp search PATH."""
    exe = _bundled_exe("ffmpeg", "ffmpeg")
    return exe.parent if exe else None


def deno_exe() -> Path | None:
    return _bundled_exe("deno", "deno")
