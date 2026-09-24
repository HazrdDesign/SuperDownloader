"""Entry point: logging, engine bootstrap, then the main window.

``--selftest <file.json>`` runs a headless check of the packaged app (engine
version and source, bundled FFmpeg/Deno) and writes the result to a file. The
build script and CI use it because a ``--windowed`` exe has no console.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import subprocess
import sys
import traceback
from pathlib import Path

if __package__ in (None, ""):
    # Running as a script (python app/main.py) or as the PyInstaller entry point.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "app"

from app import paths, updater  # noqa: E402  (must not import yt_dlp yet)

log = logging.getLogger("app")


class RedactFilter(logging.Filter):
    """Last line of defence: mask passwords and cookie headers in every log record."""

    def __init__(self) -> None:
        super().__init__()
        self._redact = None

    def attach(self, redact) -> None:
        self._redact = redact

    def filter(self, record: logging.LogRecord) -> bool:
        if self._redact is not None:
            try:
                msg = record.getMessage()
            except Exception:  # noqa: BLE001
                return True
            clean = self._redact(msg)
            if clean != msg:
                record.msg, record.args = clean, ()
        return True


REDACTOR = RedactFilter()


def setup_logging() -> Path:
    log_dir = paths.logs_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        log_dir / "app.log", maxBytes=1_000_000, backupCount=5, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    handler.addFilter(REDACTOR)
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if os.environ.get("VD_DEBUG") else logging.INFO)
    root.addHandler(handler)
    if not getattr(sys, "frozen", False) and sys.stderr:
        console = logging.StreamHandler()
        console.setLevel(logging.WARNING)
        console.addFilter(REDACTOR)
        root.addHandler(console)
    return log_dir


def _tool_version(exe: Path | None, *args: str) -> str | None:
    if not exe:
        return None
    try:
        out = subprocess.run([str(exe), *args], capture_output=True, text=True, timeout=30,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return (out.stdout or out.stderr).strip().splitlines()[0]
    except Exception as e:  # noqa: BLE001
        return f"error: {e}"


def selftest(out_file: str) -> int:
    state = updater.bootstrap_engine()
    from app import engine
    import yt_dlp
    try:
        import yt_dlp_ejs
        ejs = yt_dlp_ejs.version
    except Exception as e:  # noqa: BLE001
        ejs = f"missing: {e}"
    ff = paths.ffmpeg_dir()
    ffmpeg = ff / ("ffmpeg.exe" if sys.platform == "win32" else "ffmpeg") if ff else None
    ffprobe = ff / ("ffprobe.exe" if sys.platform == "win32" else "ffprobe") if ff else None
    report = {
        "frozen": bool(getattr(sys, "frozen", False)),
        "python": sys.version.split()[0],
        "engine_version": engine.engine_version(),
        "engine_source": state.source,
        "engine_error": state.error,
        "engine_file": getattr(yt_dlp, "__file__", None),
        "ejs_version": ejs,
        "ffmpeg": _tool_version(ffmpeg, "-version"),
        "ffprobe": _tool_version(ffprobe, "-version"),
        "deno": _tool_version(paths.deno_exe(), "--version"),
        "downloads_dir": str(paths.downloads_dir()),
        "app_data_dir": str(paths.app_data_dir()),
    }
    try:
        import customtkinter  # noqa: F401
        import PIL  # noqa: F401
        report["ui_imports"] = "ok"
    except Exception as e:  # noqa: BLE001
        report["ui_imports"] = f"error: {e}"
    Path(out_file).write_text(json.dumps(report, indent=2), encoding="utf-8")
    ok = all(report[k] and not str(report[k]).startswith(("error", "missing"))
             for k in ("engine_version", "ejs_version", "ffmpeg", "ffprobe", "deno", "ui_imports"))
    return 0 if ok else 1


def run() -> int:
    setup_logging()
    log.info("Starting (frozen=%s, python=%s)", bool(getattr(sys, "frozen", False)), sys.version.split()[0])

    # Must happen before anything imports yt_dlp.
    state = updater.bootstrap_engine()
    log.info("Engine yt-dlp %s (%s)%s", state.version, state.source,
             f"; update not used: {state.error}" if state.error else "")

    from app import engine, settings as settings_mod
    REDACTOR.attach(engine.redact)

    import customtkinter as ctk
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")

    from app.ui_main import MainWindow

    cfg = settings_mod.load()
    app = MainWindow(cfg)

    def report_callback_exception(exc, val, tb):
        log.error("Unhandled UI error:\n%s", "".join(traceback.format_exception(exc, val, tb)))
        try:
            from tkinter import messagebox
            messagebox.showerror("Video Downloader", "Something went wrong. Details were saved to the log "
                                                     "(Settings → Open logs).", parent=app)
        except Exception:  # noqa: BLE001
            pass

    app.report_callback_exception = report_callback_exception
    app.mainloop()
    log.info("Exited")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) >= 2 and argv[0] == "--selftest":
        try:
            return selftest(argv[1])
        except Exception:  # noqa: BLE001
            Path(argv[1]).write_text(json.dumps({"crash": traceback.format_exc()}), encoding="utf-8")
            return 2
    try:
        return run()
    except Exception:  # noqa: BLE001
        logging.getLogger("app").critical("Fatal error:\n%s", traceback.format_exc())
        raise


if __name__ == "__main__":
    sys.exit(main())
