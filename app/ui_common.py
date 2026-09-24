"""Small UI helpers shared by the main and settings windows."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import tkinter as tk
from pathlib import Path

import customtkinter as ctk

from . import paths
from .errors import Severity

log = logging.getLogger(__name__)

# Banner colors: (background, accent) per severity, tuned for the dark theme.
BANNER_COLORS = {
    Severity.OK: ("#12351f", "#2fbf71"),
    Severity.WARN: ("#3a3212", "#e5b93a"),
    Severity.ERROR: ("#3d1717", "#e5534b"),
}
# A colored dot in the accent color (Tk 8.6 cannot reliably draw emoji on Windows).
BANNER_DOT = "●"
MUTED = ("gray40", "gray65")
LINK = ("#1f6aa5", "#6cb4ee")
PAD = 16


class Tooltip:
    """Minimal hover tooltip for any widget."""

    def __init__(self, widget: tk.Misc, text: str, delay_ms: int = 400, wrap: int = 320):
        self.widget, self.text, self.delay, self.wrap = widget, text, delay_ms, wrap
        self._after: str | None = None
        self._tip: tk.Toplevel | None = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _event=None):
        self._cancel()
        self._after = self.widget.after(self.delay, self._show)

    def _cancel(self):
        if self._after:
            self.widget.after_cancel(self._after)
            self._after = None

    def _show(self):
        if self._tip or not self.widget.winfo_exists():
            return
        x = self.widget.winfo_rootx() + 10
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        self._tip = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")
        tk.Label(tw, text=self.text, justify="left", wraplength=self.wrap, background="#2b2b2b",
                 foreground="#eeeeee", relief="solid", borderwidth=1, padx=8, pady=6,
                 font=("Segoe UI", 9) if sys.platform == "win32" else None).pack()

    def _hide(self, _event=None):
        self._cancel()
        if self._tip:
            self._tip.destroy()
            self._tip = None


def set_window_icon(window: tk.Misc) -> None:
    """Apply the app icon. CustomTkinter resets the icon shortly after creation, so re-apply."""
    ico = paths.asset("icon.ico")
    png = paths.asset("icon.png")

    def apply():
        try:
            if sys.platform == "win32" and ico.is_file():
                window.iconbitmap(str(ico))
            elif png.is_file():
                img = tk.PhotoImage(file=str(png))
                window.iconphoto(True, img)
                window._vd_icon = img  # keep a reference
        except tk.TclError as e:
            log.debug("Could not set window icon: %s", e)

    apply()
    window.after(250, apply)


def show_in_folder(path: Path) -> None:
    """Open the file manager with ``path`` selected (Explorer on Windows)."""
    path = Path(path)
    try:
        if sys.platform == "win32":
            if path.is_file():
                subprocess.Popen(f'explorer /select,"{path}"')
            else:
                os.startfile(str(path if path.is_dir() else path.parent))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", str(path)] if path.is_file() else ["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path.parent if path.is_file() else path)])
    except OSError as e:
        log.warning("Could not open file manager: %s", e)


def open_folder(path: Path) -> None:
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    show_in_folder(path)


def elide_middle(text: str, max_chars: int = 48) -> str:
    if len(text) <= max_chars:
        return text
    keep = max_chars - 1
    return text[: keep // 2] + "…" + text[-(keep - keep // 2):]


def link_label(master, text: str, command) -> ctk.CTkLabel:
    lbl = ctk.CTkLabel(master, text=text, text_color=LINK, cursor="hand2")
    lbl.bind("<Button-1>", lambda _e: command())
    return lbl
