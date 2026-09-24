"""Settings window (gear icon)."""

from __future__ import annotations

import dataclasses
import gc
import logging
import queue
import threading
from pathlib import Path
from tkinter import filedialog
from typing import Callable

import customtkinter as ctk

from . import browsers, paths, updater
from .browsers import LOGIN_TOOLTIP, LoginSource
from .errors import classify_error
from .settings import QUALITY_CHOICES, QUALITY_LABELS, Settings, validate_folder
from .ui_common import MUTED, PAD, Tooltip, elide_middle, link_label, open_folder, set_window_icon

log = logging.getLogger(__name__)

ERROR_COLOR = ("#c0392b", "#ff7b72")
OK_COLOR = ("#1e8449", "#56d364")


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
        self._sources = [s for s in login_sources if not s.key.startswith("custom:")] + \
                        [s for s in login_sources if s.key.startswith("custom:")]
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

        # -- default quality ----------------------------------------------------------------
        ctk.CTkLabel(body, text="Default quality").grid(row=row, column=0, sticky="w", padx=(0, 12), pady=(10, 0))
        self.quality_menu = ctk.CTkOptionMenu(body, values=[QUALITY_LABELS[q] for q in QUALITY_CHOICES],
                                              dynamic_resizing=False)
        self.quality_menu.grid(row=row, column=1, columnspan=2, sticky="ew", pady=(10, 0))
        self.quality_menu.set(QUALITY_LABELS.get(settings.default_quality, QUALITY_LABELS["best"]))
        row += 1

        # -- default login ------------------------------------------------------------------
        lbl = ctk.CTkLabel(body, text="Default login source ⓘ")
        lbl.grid(row=row, column=0, sticky="w", padx=(0, 12), pady=(10, 0))
        Tooltip(lbl, LOGIN_TOOLTIP)
        self.login_menu = ctk.CTkOptionMenu(body, values=[s.label for s in self._sources], dynamic_resizing=False,
                                            command=lambda _v: self._update_login_note())
        self.login_menu.grid(row=row, column=1, columnspan=2, sticky="ew", pady=(10, 0))
        current = browsers.from_key(settings.login_source, self._sources)
        self.login_menu.set(current.label)
        row += 1
        self.login_note = ctk.CTkLabel(body, text="", text_color=MUTED, anchor="w", justify="left", wraplength=380)
        self.login_note.grid(row=row, column=1, columnspan=2, sticky="w")
        self._update_login_note()
        row += 1

        # -- engine -------------------------------------------------------------------------
        ctk.CTkFrame(body, height=1, fg_color=("gray75", "gray30")).grid(
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
        ctk.CTkFrame(body, height=1, fg_color=("gray75", "gray30")).grid(
            row=row, column=0, columnspan=3, sticky="ew", pady=14)
        row += 1
        link_label(body, "Open logs", lambda: open_folder(paths.logs_dir())).grid(row=row, column=0, sticky="w")
        self.error_label = ctk.CTkLabel(body, text="", text_color=ERROR_COLOR, anchor="e")
        self.error_label.grid(row=row, column=1, sticky="e", padx=8)
        buttons = ctk.CTkFrame(body, fg_color="transparent")
        buttons.grid(row=row, column=2, sticky="e")
        ctk.CTkButton(buttons, text="Cancel", width=80, fg_color="transparent", border_width=1,
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

    def _selected_source(self) -> LoginSource:
        label = self.login_menu.get()
        return next((s for s in self._sources if s.label == label), browsers.NONE_SOURCE)

    def _update_login_note(self) -> None:
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
        new = dataclasses.replace(
            self._original,
            save_folder=folder,
            default_quality=quality,
            login_source=self._selected_source().key,
            check_updates_on_launch=bool(self.auto_update_var.get()),
        )
        try:
            self._save_settings(new)
        except OSError as e:
            log.warning("Could not save settings: %s", e)
            self.error_label.configure(text="Couldn't save settings.")
            return
        log.info("Settings saved (quality=%s, login=%s, custom folder=%s)", quality,
                 "none" if self._selected_source().is_none else self._selected_source().app_name,
                 "yes" if folder else "no")
        self._on_saved(new)
        self.destroy()
