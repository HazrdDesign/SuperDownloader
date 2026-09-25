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


def ensure_std_streams() -> None:
    """A --windowed exe has no stdout/stderr (they are None); give libraries a harmless sink."""
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))  # noqa: SIM115


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
    """Headless check of the packaged app. The report is rewritten after every stage, so if
    something hangs, the file shows how far it got."""
    out = Path(out_file)
    report: dict = {"stage": "starting", "frozen": bool(getattr(sys, "frozen", False)),
                    "python": sys.version.split()[0]}

    def save(stage: str) -> None:
        report["stage"] = stage
        out.write_text(json.dumps(report, indent=2), encoding="utf-8")

    save("bootstrap")
    state = updater.bootstrap_engine()
    from app import engine
    import yt_dlp
    report.update({"engine_version": engine.engine_version(), "engine_source": state.source,
                   "engine_error": state.error, "engine_file": getattr(yt_dlp, "__file__", None)})
    try:
        import yt_dlp_ejs
        report["ejs_version"] = yt_dlp_ejs.version
    except Exception as e:  # noqa: BLE001
        report["ejs_version"] = f"missing: {e}"
    save("tools")
    ff = paths.ffmpeg_dir()
    ffmpeg = ff / ("ffmpeg.exe" if sys.platform == "win32" else "ffmpeg") if ff else None
    ffprobe = ff / ("ffprobe.exe" if sys.platform == "win32" else "ffprobe") if ff else None
    report.update({
        "ffmpeg": _tool_version(ffmpeg, "-version"),
        "ffprobe": _tool_version(ffprobe, "-version"),
        "deno": _tool_version(paths.deno_exe(), "--version"),
        "downloads_dir": str(paths.downloads_dir()),
        "app_data_dir": str(paths.app_data_dir()),
    })
    try:
        import customtkinter  # noqa: F401
        import PIL  # noqa: F401
        report["ui_imports"] = "ok"
    except Exception as e:  # noqa: BLE001
        report["ui_imports"] = f"error: {e}"
    save("engine_e2e")
    report["engine_e2e"] = _selftest_engine(ffmpeg)
    save("ui_window")
    report["ui_window"] = _selftest_window()
    save("done")
    ok = all(report.get(k) and not str(report[k]).startswith(("error", "missing"))
             for k in ("engine_version", "ejs_version", "ffmpeg", "ffprobe", "deno", "ui_imports",
                       "engine_e2e", "ui_window"))
    return 0 if ok else 1


def _selftest_engine(ffmpeg: Path | None) -> str:
    """Offline end-to-end run: make a clip with the bundled FFmpeg, serve it locally,
    check it and download it as MP4 and MP3 through the real engine."""
    import functools
    import http.server
    import tempfile
    import threading

    from app.engine import CheckRequest, DownloadJob, Engine

    if not ffmpeg:
        return "error: no bundled ffmpeg"
    try:
        src = Path(tempfile.mkdtemp(prefix="vd-selftest-src-"))
        out = Path(tempfile.mkdtemp(prefix="vd-selftest-out-"))
        subprocess.run([str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
                        "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25:duration=2",
                        "-f", "lavfi", "-i", "sine=duration=2", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                        "-c:a", "aac", "-shortest", str(src / "clip.mp4")],
                       check=True, timeout=60, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(src))
        handler.log_message = lambda *a, **k: None
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            eng = Engine()
            res = eng.check_link(CheckRequest(f"http://127.0.0.1:{server.server_address[1]}/clip.mp4"))
            if not res.ok:
                return f"error: check {res.result.status.value}: {res.result.detail[-300:]}"
            names = []
            for q in (res.qualities[0], next(q for q in res.qualities if q.audio_only)):
                r = eng.download(DownloadJob([res.url], q, out), lambda p: None)
                if not r.files:
                    return f"error: download {q.key}: {r.error.detail[-300:] if r.error else '?'}"
                names.append(r.files[0].name)
            return "ok: " + ", ".join(names)
        finally:
            server.shutdown()
    except Exception as e:  # noqa: BLE001
        return f"error: {e!r}"


def _selftest_window() -> str:
    """Create and destroy the real main window (catches missing UI data files in the exe)."""
    try:
        import customtkinter as ctk

        from app.settings import Settings
        from app.ui_main import MainWindow

        from app import theme
        from app.history import History
        theme.apply()
        win = MainWindow(Settings(), check_updates=False, save_settings=lambda s: None, warm_up_engine=False,
                         history=History(persist=False))
        win.withdraw()
        win.update()
        win.open_settings()
        win.update()
        win.destroy()
        return "ok"
    except Exception as e:  # noqa: BLE001
        return f"error: {e!r}"


def run() -> int:
    migrated_from = paths.migrate_legacy_data()  # before anything creates the new folder
    setup_logging()
    if migrated_from:
        log.info("Copied settings from %s", migrated_from)
    log.info("Starting (frozen=%s, python=%s)", bool(getattr(sys, "frozen", False)), sys.version.split()[0])

    # Must happen before anything imports yt_dlp.
    state = updater.bootstrap_engine()
    log.info("Engine yt-dlp %s (%s)%s", state.version, state.source,
             f"; update not used: {state.error}" if state.error else "")

    from app import engine, settings as settings_mod
    REDACTOR.attach(engine.redact)

    import customtkinter as ctk
    from app import theme
    theme.apply()

    from app.ui_main import MainWindow

    cfg = settings_mod.load()
    app = MainWindow(cfg)

    def report_callback_exception(exc, val, tb):
        log.error("Unhandled UI error:\n%s", "".join(traceback.format_exception(exc, val, tb)))
        try:
            from tkinter import messagebox
            messagebox.showerror("Super Downloader", "Something went wrong. Details were saved to the log "
                                                     "(Settings → Open logs).", parent=app)
        except Exception:  # noqa: BLE001
            pass

    app.report_callback_exception = report_callback_exception
    app.mainloop()
    log.info("Exited")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    ensure_std_streams()
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
