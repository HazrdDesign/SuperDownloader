"""Settings window (gear icon)."""

from __future__ import annotations

import dataclasses
import gc
import logging
import queue
import sys
import threading
from pathlib import Path
from tkinter import filedialog
from typing import Callable

import customtkinter as ctk

from . import browsers, paths, theme, updater
from .browsers import CUSTOM_LABEL, LOGIN_TOOLTIP, LoginSource
from .engine import FORMATS, format_by_key
from .errors import classify_error
from .settings import QUALITY_CHOICES, QUALITY_LABELS, Settings, validate_folder
from .ui_common import MUTED, PAD, Tooltip, elide_middle, link_label, open_folder, set_window_icon

log = logging.getLogger(__name__)

ERROR_COLOR = theme.ERROR
OK_COLOR = theme.OK
NEVER_LABEL = "Never use a browser login"


def _set_text(label: ctk.CTkLabel, text: str, **kw) -> None:
    """Set a hint label's text and hide its row entirely when there is nothing to say."""
    label.configure(text=text, **kw)
    if text:
        label.grid()
    else:
        label.grid_remove()


class SettingsWindow(ctk.CTkToplevel):
    def __init__(self, master, settings: Settings, login_sources: list[LoginSource], *,
                 on_saved: Callable[[Settings], None], on_engine_updated: Callable[[str], None] | None = None,
                 save_settings: Callable[[Settings], None]):
        super().__init__(master)
        self.title("Settings")
        set_window_icon(self)
        self.resizable(False, False)
        self.transient(master)
        self._original = settings
        self._on_saved = on_saved
        self._on_engine_updated = on_engine_updated
        self._save_settings = save_settings
        self._sources = [s for s in login_sources if not s.is_none]
        names = list(dict.fromkeys(s.app_name for s in browsers.candidates(login_sources) if s.app_name))
        if not names:
            self._auto_label = "Automatic (no browsers found yet)"
        elif len(names) == 1:
            self._auto_label = f"Automatic ({names[0]})"
        else:
            self._auto_label = f"Automatic: try each browser ({', '.join(names)})"
        self._events: queue.Queue = queue.Queue()
        self._update_info: updater.UpdateInfo | None = None
        self._folder = settings.save_folder

        self.grid_columnconfigure(0, weight=1)
        body = ctk.CTkFrame(self, fg_color="transparent")
        body.grid(row=0, column=0, sticky="nsew", padx=PAD, pady=PAD)
        body.grid_columnconfigure(1, weight=1)
        row = 0

        # -- default folder -----------------------------------------------------------------
        ctk.CTkLabel(body, text="Default save folder").grid(row=row, column=0, sticky="w", padx=(0, 12))
        self.folder_label = ctk.CTkLabel(body, text="", anchor="w", width=280)
        self.folder_label.grid(row=row, column=1, sticky="ew")
        ctk.CTkButton(body, text="Browse", width=80, command=self._browse).grid(row=row, column=2, padx=(8, 0))
        row += 1
        self.folder_hint = ctk.CTkLabel(body, text="", text_color=MUTED, anchor="w")
        self.folder_hint.grid(row=row, column=1, columnspan=2, sticky="w")
        row += 1

        # -- default quality + format -----------------------------------------------------------
        ctk.CTkLabel(body, text="Default quality").grid(row=row, column=0, sticky="w", padx=(0, 12), pady=(10, 0))
        self.quality_menu = ctk.CTkOptionMenu(body, values=[QUALITY_LABELS[q] for q in QUALITY_CHOICES],
                                              dynamic_resizing=False)
        self.quality_menu.grid(row=row, column=1, columnspan=2, sticky="ew", pady=(10, 0))
        self.quality_menu.set(QUALITY_LABELS.get(settings.default_quality, QUALITY_LABELS["best"]))
        row += 1
        ctk.CTkLabel(body, text="Default format").grid(row=row, column=0, sticky="w", padx=(0, 12), pady=(10, 0))
        self.format_menu = ctk.CTkOptionMenu(body, values=[f.label for f in FORMATS], dynamic_resizing=False)
        self.format_menu.grid(row=row, column=1, columnspan=2, sticky="ew", pady=(10, 0))
        self.format_menu.set(format_by_key(settings.default_format).label)
        row += 1

        # -- browser login ------------------------------------------------------------------
        lbl = ctk.CTkLabel(body, text="Browser for logins ⓘ")
        lbl.grid(row=row, column=0, sticky="w", padx=(0, 12), pady=(10, 0))
        Tooltip(lbl, LOGIN_TOOLTIP + " Used for private or password-protected videos, and whenever a site asks "
                     "you to sign in.")
        self.login_menu = ctk.CTkOptionMenu(body, values=self._login_labels(), dynamic_resizing=False,
                                            command=self._on_login_selected)
        self.login_menu.grid(row=row, column=1, columnspan=2, sticky="ew", pady=(10, 0))
        self._login_key = settings.login_source
        self.login_menu.set(self._label_for_key(settings.login_source))
        row += 1
        self.login_note = ctk.CTkLabel(body, text="", text_color=MUTED, anchor="w", justify="left", wraplength=380)
        self.login_note.grid(row=row, column=1, columnspan=2, sticky="w")
        self._update_login_note()
        row += 1

        # -- conveniences -------------------------------------------------------------------
        self.copied_var = ctk.BooleanVar(value=settings.use_copied_links)
        ctk.CTkCheckBox(body, text="Use a video link I just copied when I switch to the app",
                        variable=self.copied_var).grid(row=row, column=1, columnspan=2, sticky="w", pady=(10, 0))
        row += 1
        self.notify_var = ctk.BooleanVar(value=settings.notify_when_done)
        ctk.CTkCheckBox(body, text="Flash the taskbar and play a sound when a download finishes",
                        variable=self.notify_var).grid(row=row, column=1, columnspan=2, sticky="w", pady=(6, 0))
        row += 1

        # -- engine -------------------------------------------------------------------------
        ctk.CTkFrame(body, height=1, fg_color=theme.BORDER).grid(
            row=row, column=0, columnspan=3, sticky="ew", pady=14)
        row += 1
        ctk.CTkLabel(body, text="Downloader engine").grid(row=row, column=0, sticky="w", padx=(0, 12))
        st = updater.STATE
        where = " (updated)" if st.source == "updated" else ""
        self.engine_label = ctk.CTkLabel(body, text=f"yt-dlp {st.version or '?'}{where}", anchor="w")
        self.engine_label.grid(row=row, column=1, sticky="w")
        self.update_btn = ctk.CTkButton(body, text="Check for update", width=140, command=self._check_update)
        self.update_btn.grid(row=row, column=2, padx=(8, 0))
        row += 1
        self.engine_status = ctk.CTkLabel(body, text="", anchor="w", justify="left", wraplength=380)
        self.engine_status.grid(row=row, column=1, columnspan=2, sticky="w")
        row += 1
        self.auto_update_var = ctk.BooleanVar(value=settings.check_updates_on_launch)
        ctk.CTkCheckBox(body, text="Check for engine updates when the app starts",
                        variable=self.auto_update_var).grid(row=row, column=1, columnspan=2, sticky="w", pady=(6, 0))
        row += 1

        # -- logs + buttons -----------------------------------------------------------------
        ctk.CTkFrame(body, height=1, fg_color=theme.BORDER).grid(
            row=row, column=0, columnspan=3, sticky="ew", pady=14)
        row += 1
        link_label(body, "Open logs", lambda: open_folder(paths.logs_dir())).grid(row=row, column=0, sticky="w")
        self.error_label = ctk.CTkLabel(body, text="", text_color=ERROR_COLOR, anchor="e")
        self.error_label.grid(row=row, column=1, sticky="e", padx=8)
        buttons = ctk.CTkFrame(body, fg_color="transparent")
        buttons.grid(row=row, column=2, sticky="e")
        ctk.CTkButton(buttons, text="Cancel", width=80, fg_color="transparent", border_width=1,
                      border_color=theme.BORDER, text_color=theme.TEXT, hover_color=theme.SURFACE_2,
                      command=self.destroy).pack(side="left", padx=(0, 8))
        self.save_btn = ctk.CTkButton(buttons, text="Save", width=80, command=self.save)
        self.save_btn.pack(side="left")

        self._refresh_folder()
        _set_text(self.engine_status, "")
        self.bind("<Escape>", lambda _e: self.destroy())
        self.bind("<Return>", lambda _e: self.save())
        self.after(100, self._grab)
        self.after(50, self._poll)

    def destroy(self) -> None:
        super().destroy()
        # Collect this window's Tk objects (fonts, images) now, on the UI thread. Otherwise a
        # background thread may collect them later and have to wait on the UI thread.
        gc.collect()

    def _grab(self) -> None:
        try:
            self.lift()
            self.focus_force()
            self.grab_set()
        except Exception:  # noqa: BLE001 - window may already be closed
            pass

    # -- folder -------------------------------------------------------------------------------

    def _refresh_folder(self) -> None:
        effective = Path(self._folder) if self._folder else paths.downloads_dir()
        self.folder_label.configure(text=elide_middle(str(effective), 46))
        _set_text(self.folder_hint, "" if self._folder else "Your Downloads folder")

    def _browse(self) -> None:
        start = self._folder or str(paths.downloads_dir())
        folder = filedialog.askdirectory(parent=self, title="Default save folder", initialdir=start, mustexist=True)
        if folder:
            self._folder = str(Path(folder))
            self.error_label.configure(text="")
            self._refresh_folder()

    # -- login --------------------------------------------------------------------------------

    def _login_labels(self) -> list[str]:
        return [self._auto_label] + [s.label for s in self._sources] + [CUSTOM_LABEL, NEVER_LABEL]

    def _label_for_key(self, key: str) -> str:
        if not key or key == browsers.AUTO_KEY:
            return self._auto_label
        if key == browsers.NONE_KEY:
            return NEVER_LABEL
        src = browsers.from_key(key, self._sources)
        if src.is_none:
            return self._auto_label
        if src.key not in {s.key for s in self._sources}:
            self._sources.append(src)
            self.login_menu.configure(values=self._login_labels())
        return src.label

    def _on_login_selected(self, label: str) -> None:
        if label == CUSTOM_LABEL:
            folder = filedialog.askdirectory(parent=self, title="Choose a browser profile folder", mustexist=True)
            src = browsers.custom_source(folder) if folder else None
            if not src:
                if folder:
                    _set_text(self.login_note, "That folder has no saved browser login (cookies.sqlite). Choose a "
                                               "Firefox, Zen, LibreWolf or Floorp profile folder.")
                self.login_menu.set(self._label_for_key(self._login_key))
                return
            self._sources.append(src)
            self.login_menu.configure(values=self._login_labels())
            self.login_menu.set(src.label)
            self._login_key = src.key
        elif label == self._auto_label:
            self._login_key = browsers.AUTO_KEY
        elif label == NEVER_LABEL:
            self._login_key = browsers.NONE_KEY
        else:
            self._login_key = next((s.key for s in self._sources if s.label == label), browsers.AUTO_KEY)
        self._update_login_note()

    def _selected_source(self) -> LoginSource:
        return browsers.resolve(self._login_key, self._sources)

    def _update_login_note(self) -> None:
        if self._login_key == browsers.NONE_KEY:
            _set_text(self.login_note, "Private videos won't download. Sites that ask for a login will show a message.")
            return
        if self._login_key in (None, "", browsers.AUTO_KEY):
            blocked = sys.platform == "win32" and any(src.is_chromium for src in self._sources)
            _set_text(self.login_note, "When a site needs a login, each browser is tried in turn, the one you used "
                                       "most recently first."
                                       + (" Chrome-based browsers go last: Windows usually blocks reading them."
                                          if blocked else ""))
            return
        _set_text(self.login_note, self._selected_source().note)

    # -- engine update ------------------------------------------------------------------------

    def _check_update(self) -> None:
        if self._update_info and self._update_info.available:
            return self._install_update()
        self.update_btn.configure(state="disabled", text="Checking…")
        _set_text(self.engine_status, "", text_color=MUTED)

        def work():
            try:
                self._events.put(("checked", updater.check_for_update()))
            except Exception as e:  # noqa: BLE001
                self._events.put(("error", e))

        threading.Thread(target=work, name="settings-update-check", daemon=True).start()

    def _install_update(self) -> None:
        info = self._update_info
        self.update_btn.configure(state="disabled", text="Updating…")

        def work():
            try:
                updater.install_update(info)
                self._events.put(("installed", info.latest))
            except Exception as e:  # noqa: BLE001
                self._events.put(("error", e))

        threading.Thread(target=work, name="settings-update-install", daemon=True).start()

    def _poll(self) -> None:
        if not self.winfo_exists():
            return
        try:
            while True:
                kind, payload = self._events.get_nowait()
                if kind == "checked":
                    self._update_info = payload
                    if payload.available:
                        _set_text(self.engine_status, f"Version {payload.latest} is available.",
                                                     text_color=MUTED)
                        self.update_btn.configure(state="normal", text="Update")
                    else:
                        _set_text(self.engine_status, "Up to date", text_color=OK_COLOR)
                        self.update_btn.configure(state="normal", text="Check for update")
                elif kind == "installed":
                    _set_text(self.engine_status, "Updated — restart the app.", text_color=OK_COLOR)
                    self.update_btn.configure(state="disabled", text="Updated")
                    self._update_info = None
                    if self._on_engine_updated:
                        self._on_engine_updated(payload)
                elif kind == "error":
                    info = classify_error(payload)
                    log.warning("Engine update check failed: %s", info.detail)
                    msg = ("Couldn't reach the update server. Check your internet connection."
                           if info.status.value in ("network", "unknown") else info.message)
                    _set_text(self.engine_status, msg, text_color=ERROR_COLOR)
                    self.update_btn.configure(state="normal", text="Check for update")
        except queue.Empty:
            pass
        self.after(100, self._poll)

    # -- save ---------------------------------------------------------------------------------

    def save(self) -> None:
        folder = self._folder
        if folder:
            problem = validate_folder(folder)
            if problem:
                self.error_label.configure(text=problem)
                return
        quality_label = self.quality_menu.get()
        quality = next((k for k, v in QUALITY_LABELS.items() if v == quality_label), "best")
        fmt = next((f.key for f in FORMATS if f.label == self.format_menu.get()), "original")
        new = dataclasses.replace(
            self._original,
            save_folder=folder,
            default_quality=quality,
            default_format=fmt,
            login_source=self._login_key or browsers.AUTO_KEY,
            check_updates_on_launch=bool(self.auto_update_var.get()),
            use_copied_links=bool(self.copied_var.get()),
            notify_when_done=bool(self.notify_var.get()),
        )
        try:
            self._save_settings(new)
        except OSError as e:
            log.warning("Could not save settings: %s", e)
            self.error_label.configure(text="Couldn't save settings.")
            return
        log.info("Settings saved (quality=%s, format=%s, login=%s, custom folder=%s)", quality, fmt,
                 new.login_source if new.login_source in ("auto", "none") else self._selected_source().app_name,
                 "yes" if folder else "no")
        self._on_saved(new)
        self.destroy()
