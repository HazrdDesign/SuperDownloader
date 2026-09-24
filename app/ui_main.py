"""Main window: paste a link, see whether it works, pick a quality, download."""

from __future__ import annotations

import io
import logging
import queue
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox
from typing import Callable
from urllib.parse import urlparse

import customtkinter as ctk

from . import browsers, settings as settings_mod, updater
from .browsers import CUSTOM_LABEL, LOGIN_TOOLTIP, LoginSource
from .engine import (
    CheckRequest,
    CheckResult,
    DownloadJob,
    DownloadResult,
    Engine,
    Progress,
    default_quality,
    fetch_thumbnail,
    human_duration,
    human_size,
    normalize_url,
    safe_folder_name,
)
from .errors import ErrorInfo, Severity, Status
from .settings import Settings
from .ui_common import (
    BANNER_COLORS,
    BANNER_DOT,
    MUTED,
    PAD,
    Tooltip,
    elide_middle,
    link_label,
    set_window_icon,
    show_in_folder,
)

log = logging.getLogger(__name__)

APP_TITLE = "Video Downloader"
DEBOUNCE_MS = 600
POLL_MS = 50
MIN_WIDTH = 520

LOGIN_STATES = {Status.NEEDS_LOGIN, Status.COOKIES_LOCKED, Status.COOKIES_UNREADABLE, Status.COOKIES_MISSING}

# Sections in the order they appear on screen.
SECTIONS = ("header", "url", "spinner", "banner", "login", "password", "referer", "preview",
            "playlist", "options", "download", "progress", "done", "update")


class MainWindow(ctk.CTk):
    def __init__(self, settings: Settings, engine: Engine | None = None, *,
                 login_sources: list[LoginSource] | None = None, check_updates: bool | None = None,
                 save_settings: Callable[[Settings], None] | None = None, warm_up_engine: bool = True):
        super().__init__()
        # Background threads run CPU-heavy yt-dlp code. A short GIL switch interval keeps
        # Tk's many small Python callbacks from queueing behind it (default is 5 ms).
        sys.setswitchinterval(0.001)
        self.settings = settings
        self.engine = engine or Engine()
        self._save_settings = save_settings or settings_mod.save
        self.events: queue.Queue = queue.Queue()
        self.stage = "empty"  # empty, checking, checked, downloading, done
        self.result: CheckResult | None = None
        self.last_error: ErrorInfo | None = None
        self.saved_files: list[Path] = []
        self.folder_override: Path | None = None
        self._token = 0
        self._debounce: str | None = None
        self._checked_url: str | None = None
        self._download_thread: threading.Thread | None = None
        self._visible: set[str] = {"header", "url"}
        self._thumb_image: ctk.CTkImage | None = None
        self.banner_status: Status | None = None

        self.login_sources = login_sources if login_sources is not None else browsers.all_sources()
        self.login = browsers.from_key(settings.login_source, self.login_sources)

        self.title(APP_TITLE)
        set_window_icon(self)
        self.minsize(MIN_WIDTH, 200)
        self._restore_geometry()
        self._build()
        self._bind_keys()
        self._relayout()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._poll_id = self.after(POLL_MS, self._poll)
        self.after(100, self.url_entry.focus_set)
        self.after(200, self._fit_height)

        if check_updates is None:
            check_updates = settings.check_updates_on_launch
        if check_updates:
            self._check_engine_update_async()
        if warm_up_engine and hasattr(self.engine, "check_link"):
            from .engine import warm_up
            self.after(300, lambda: threading.Thread(target=warm_up, name="warm-up", daemon=True).start())

    # ======================================================================================
    # Layout
    # ======================================================================================

    def _build(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.body = ctk.CTkFrame(self, fg_color="transparent")
        self.body.grid(row=0, column=0, sticky="nsew", padx=PAD, pady=(PAD - 4, PAD))
        self.body.grid_columnconfigure(0, weight=1)
        b = self.body
        self.sections: dict[str, ctk.CTkFrame] = {}

        # -- header -------------------------------------------------------------------------
        header = self._section("header")
        header.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(header, text=APP_TITLE, font=ctk.CTkFont(size=18, weight="bold")).grid(
            row=0, column=0, sticky="w")
        self.settings_btn = ctk.CTkButton(header, text="⚙", width=36, height=32, font=ctk.CTkFont(size=18),
                                          fg_color="transparent", hover_color=("gray80", "gray25"),
                                          command=self.open_settings)
        self.settings_btn.grid(row=0, column=1, sticky="e")
        Tooltip(self.settings_btn, "Settings")

        # -- URL ----------------------------------------------------------------------------
        url = self._section("url")
        url.grid_columnconfigure(0, weight=1)
        self.url_var = tk.StringVar()
        self.url_entry = ctk.CTkEntry(url, textvariable=self.url_var, height=38,
                                      placeholder_text="Paste a video link here")
        self.url_entry.grid(row=0, column=0, sticky="ew")
        self.paste_btn = ctk.CTkButton(url, text="Paste", width=70, height=38, command=self.paste_url)
        self.paste_btn.grid(row=0, column=1, padx=(8, 0))
        self.check_btn = ctk.CTkButton(url, text="Check", width=70, height=38, command=self.start_check,
                                       fg_color="transparent", border_width=1)
        self.check_btn.grid(row=0, column=2, padx=(8, 0))
        self.url_var.trace_add("write", self._on_url_changed)

        # -- spinner ------------------------------------------------------------------------
        spinner = self._section("spinner")
        spinner.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(spinner, text="Checking link…", text_color=MUTED).grid(row=0, column=0, padx=(0, 10))
        self.spinner_bar = ctk.CTkProgressBar(spinner, mode="indeterminate", height=6)
        self.spinner_bar.grid(row=0, column=1, sticky="ew")

        # -- banner -------------------------------------------------------------------------
        banner = self.banner = self._section("banner", corner_radius=8)
        banner.grid_columnconfigure(1, weight=1)
        self.banner_dot = ctk.CTkLabel(banner, text=BANNER_DOT, font=ctk.CTkFont(size=16), width=20)
        self.banner_dot.grid(row=0, column=0, sticky="nw", padx=(12, 6), pady=10)
        self.banner_text = ctk.CTkLabel(banner, text="", justify="left", anchor="w", wraplength=420)
        self.banner_text.grid(row=0, column=1, sticky="ew", padx=(0, 12), pady=10)
        self.details_link = link_label(banner, "Details ▸", self.toggle_details)
        self.details_box = ctk.CTkTextbox(banner, height=110, wrap="word", font=ctk.CTkFont(family="Consolas", size=11))
        banner.bind("<Configure>", self._on_banner_resize)

        # -- login --------------------------------------------------------------------------
        login = self._section("login")
        login.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(login, text="Use login from").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.login_menu = ctk.CTkOptionMenu(login, values=self._login_labels(), command=self._on_login_selected,
                                            dynamic_resizing=False)
        self.login_menu.grid(row=0, column=1, sticky="ew")
        info = ctk.CTkLabel(login, text="ⓘ", text_color=MUTED, width=20, cursor="question_arrow")
        info.grid(row=0, column=2, padx=(6, 0))
        Tooltip(info, LOGIN_TOOLTIP)
        self.login_retry = ctk.CTkButton(login, text="Retry", width=70, command=self.retry)
        self.login_note = ctk.CTkLabel(login, text="", text_color=MUTED, justify="left", anchor="w", wraplength=440)
        self.login_menu.set(self.login.label)
        self._update_login_note()

        # -- password -----------------------------------------------------------------------
        pw = self._section("password")
        pw.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(pw, text="Password").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.password_var = tk.StringVar()
        self.password_entry = ctk.CTkEntry(pw, textvariable=self.password_var, show="•",
                                           placeholder_text="Only if you were given the password")
        self.password_entry.grid(row=0, column=1, sticky="ew")
        ctk.CTkButton(pw, text="Retry", width=70, command=self.retry).grid(row=0, column=2, padx=(8, 0))
        self.password_entry.bind("<Return>", lambda _e: (self.retry(), "break")[1])

        # -- referer ------------------------------------------------------------------------
        ref = self._section("referer")
        ref.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(ref, text="Page it plays on").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.referer_var = tk.StringVar()
        self.referer_entry = ctk.CTkEntry(ref, textvariable=self.referer_var,
                                          placeholder_text="https://the-site-where-it-plays.com/page")
        self.referer_entry.grid(row=0, column=1, sticky="ew")
        ctk.CTkButton(ref, text="Retry", width=70, command=self.retry).grid(row=0, column=2, padx=(8, 0))
        self.referer_entry.bind("<Return>", lambda _e: (self.retry(), "break")[1])

        # -- preview ------------------------------------------------------------------------
        prev = self._section("preview", corner_radius=8, fg_color=("gray90", "gray17"))
        prev.grid_columnconfigure(1, weight=1)
        self.thumb_label = ctk.CTkLabel(prev, text="", width=160, height=90, fg_color=("gray80", "gray22"),
                                        corner_radius=6)
        self.thumb_label.grid(row=0, column=0, rowspan=2, padx=10, pady=10, sticky="nw")
        self.title_label = ctk.CTkLabel(prev, text="", font=ctk.CTkFont(size=14, weight="bold"),
                                        justify="left", anchor="w", wraplength=300)
        self.title_label.grid(row=0, column=1, sticky="new", padx=(0, 10), pady=(10, 0))
        self.meta_label = ctk.CTkLabel(prev, text="", text_color=MUTED, justify="left", anchor="w", wraplength=300)
        self.meta_label.grid(row=1, column=1, sticky="nw", padx=(0, 10), pady=(2, 10))

        # -- playlist -----------------------------------------------------------------------
        pl = self._section("playlist")
        pl.grid_columnconfigure(0, weight=1)
        self.playlist_label = ctk.CTkLabel(pl, text="", justify="left", anchor="w", wraplength=460)
        self.playlist_label.grid(row=0, column=0, sticky="w")
        self.playlist_choice = ctk.CTkSegmentedButton(pl, values=["Just this video", "All videos"])
        self.playlist_choice.grid(row=1, column=0, sticky="w", pady=(6, 0))

        # -- options (quality + save to) ------------------------------------------------------
        opt = self._section("options")
        opt.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(opt, text="Quality").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.quality_menu = ctk.CTkOptionMenu(opt, values=["—"], dynamic_resizing=False)
        self.quality_menu.grid(row=0, column=1, columnspan=2, sticky="ew")
        ctk.CTkLabel(opt, text="Save to").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=(8, 0))
        self.folder_label = ctk.CTkLabel(opt, text="", anchor="w", text_color=MUTED)
        self.folder_label.grid(row=1, column=1, sticky="ew", pady=(8, 0))
        ctk.CTkButton(opt, text="Change", width=70, fg_color="transparent", border_width=1,
                      command=self.change_folder).grid(row=1, column=2, padx=(8, 0), pady=(8, 0))

        # -- download -----------------------------------------------------------------------
        dl = self._section("download")
        dl.grid_columnconfigure(0, weight=1)
        self.download_btn = ctk.CTkButton(dl, text="Download", height=42, font=ctk.CTkFont(size=15, weight="bold"),
                                          command=self.start_download)
        self.download_btn.grid(row=0, column=0, sticky="ew")

        # -- progress -----------------------------------------------------------------------
        prog = self._section("progress")
        prog.grid_columnconfigure(0, weight=1)
        self.progress_bar = ctk.CTkProgressBar(prog, height=12)
        self.progress_bar.grid(row=0, column=0, sticky="ew")
        self.progress_bar.set(0)
        self.cancel_btn = ctk.CTkButton(prog, text="Cancel", width=80, fg_color="transparent", border_width=1,
                                        command=self.cancel_download)
        self.cancel_btn.grid(row=0, column=1, padx=(10, 0))
        self.progress_text = ctk.CTkLabel(prog, text="", text_color=MUTED, anchor="w")
        self.progress_text.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(4, 0))

        # -- done ---------------------------------------------------------------------------
        done = self._section("done", corner_radius=8, fg_color=BANNER_COLORS[Severity.OK][0])
        done.grid_columnconfigure(0, weight=1)
        self.done_text = ctk.CTkLabel(done, text="", justify="left", anchor="w", wraplength=460,
                                      font=ctk.CTkFont(weight="bold"))
        self.done_text.grid(row=0, column=0, columnspan=2, sticky="ew", padx=12, pady=(10, 6))
        self.show_btn = ctk.CTkButton(done, text="Show in folder", command=self.show_saved)
        self.show_btn.grid(row=1, column=0, sticky="w", padx=12, pady=(0, 12))
        ctk.CTkButton(done, text="Download another", fg_color="transparent", border_width=1,
                      command=self.download_another).grid(row=1, column=1, sticky="e", padx=12, pady=(0, 12))

        # -- engine update notice -----------------------------------------------------------
        upd = self._section("update")
        upd.grid_columnconfigure(0, weight=1)
        self.update_text = ctk.CTkLabel(upd, text="", text_color=MUTED, anchor="w", justify="left", wraplength=380)
        self.update_text.grid(row=0, column=0, sticky="ew")
        self.update_btn = ctk.CTkButton(upd, text="Update", width=80, command=self._install_engine_update_async)
        self.update_btn.grid(row=0, column=1, padx=(8, 0))
        self._pending_update: updater.UpdateInfo | None = None

        self._refresh_folder_label()
        del b

    def _section(self, name: str, **kw) -> ctk.CTkFrame:
        kw.setdefault("fg_color", "transparent")
        frame = ctk.CTkFrame(self.body, **kw)
        self.sections[name] = frame
        return frame

    def _set_visible(self, *names: str, visible: bool = True) -> None:
        for n in names:
            (self._visible.add if visible else self._visible.discard)(n)

    def _only(self, *names: str) -> None:
        """Show exactly the header, URL row and the given sections."""
        self._visible = {"header", "url", *names}
        if self._pending_update is not None or self.update_text.cget("text"):
            self._visible.add("update")

    def _relayout(self) -> None:
        if "login" in self._visible and self.login.is_none and not self._login_needed():
            self._visible.discard("login")
        for i, name in enumerate(SECTIONS):
            frame = self.sections[name]
            if name in self._visible:
                pady = (0, 8) if name == "header" else (10, 0)
                frame.grid(row=i, column=0, sticky="ew", pady=pady)
            else:
                frame.grid_remove()
        self.spinner_bar.start() if "spinner" in self._visible else self.spinner_bar.stop()
        self._fit_height()

    def _fit_height(self) -> None:
        """Size the window height to its content as sections appear and disappear.

        Width and position stay wherever the user put them (and are remembered).
        """
        try:
            if not self.winfo_ismapped():
                return
            self.update_idletasks()
            need = self.winfo_reqheight()
            if need > 1 and abs(need - self.winfo_height()) > 2:
                tk.Tk.geometry(self, f"{self.winfo_width()}x{need}")
        except tk.TclError:
            pass

    def _on_banner_resize(self, event) -> None:
        wrap = max(200, event.width - 60)
        self.banner_text.configure(wraplength=wrap)
        for lbl in (self.playlist_label, self.done_text, self.login_note):
            lbl.configure(wraplength=wrap)
        self.title_label.configure(wraplength=max(160, event.width - 210))
        self.meta_label.configure(wraplength=max(160, event.width - 210))

    # ======================================================================================
    # Banner
    # ======================================================================================

    def show_banner(self, info: ErrorInfo) -> None:
        sev = info.severity
        bg, accent = BANNER_COLORS[sev]
        self.banner.configure(fg_color=bg)
        self.banner_dot.configure(text_color=accent)
        self.banner_text.configure(text=info.message)
        self.banner_status = info.status
        self.details_box.grid_remove()
        self.details_link.configure(text="Details ▸")
        if info.detail and sev is not Severity.OK:
            self.details_box.configure(state="normal")
            self.details_box.delete("1.0", "end")
            self.details_box.insert("1.0", info.detail)
            self.details_box.configure(state="disabled")
            self.details_link.grid(row=1, column=1, sticky="w", padx=(0, 12), pady=(0, 8))
        else:
            self.details_link.grid_remove()
        self._set_visible("banner")

    def toggle_details(self) -> None:
        if self.details_box.winfo_ismapped():
            self.details_box.grid_remove()
            self.details_link.configure(text="Details ▸")
        else:
            self.details_box.grid(row=2, column=0, columnspan=2, sticky="ew", padx=12, pady=(0, 12))
            self.details_link.configure(text="Details ▾")
        self._fit_height()

    # ======================================================================================
    # URL + check
    # ======================================================================================

    def paste_url(self) -> None:
        try:
            text = self.clipboard_get()
        except tk.TclError:
            text = ""
        if self.stage == "downloading":
            return
        self.url_var.set(text.strip())
        self.url_entry.icursor("end")
        self.url_entry.focus_set()
        if text.strip():
            self.start_check()

    def _on_url_changed(self, *_):
        if self.stage == "downloading":
            return
        if self._debounce:
            self.after_cancel(self._debounce)
            self._debounce = None
        text = self.url_var.get()
        url = normalize_url(text)
        if url != self._checked_url and self.stage in ("checked", "checking", "done"):
            self._token += 1  # any running check is now stale
            self._clear_result()
            self.stage = "empty"
            self._only()
            self._relayout()
        if url and url != self._checked_url:
            self._debounce = self.after(DEBOUNCE_MS, self.start_check)

    def retry(self) -> None:
        self.start_check()

    def start_check(self) -> None:
        if self.stage == "downloading":
            return
        if self._debounce:
            self.after_cancel(self._debounce)
            self._debounce = None
        text = self.url_var.get().strip()
        if not text:
            self.url_entry.focus_set()
            return
        self._token += 1
        token = self._token
        self._checked_url = normalize_url(text)
        req = CheckRequest(
            url=text,
            login=self.login,
            password=self.password_var.get() if "password" in self._visible and self.password_var.get() else None,
            referer=self.referer_var.get().strip() if "referer" in self._visible and self.referer_var.get().strip() else None,
        )
        keep = {n for n in ("login", "password", "referer") if n in self._visible}
        self._clear_result()
        self.stage = "checking"
        self._only("spinner", *keep)
        self._relayout()
        host = urlparse(self._checked_url or "").hostname or "?"
        log.info("Check started for %s", host)

        def work():
            res = self.engine.check_link(req)
            self.events.put(("check", token, res))
            if res.ok and res.video and res.video.thumbnail:
                self.events.put(("thumb", token, fetch_thumbnail(res.video.thumbnail)))

        threading.Thread(target=work, name="check", daemon=True).start()

    def _clear_result(self) -> None:
        self.result = None
        self.last_error = None
        self.saved_files = []
        self._thumb_image = None
        self.thumb_label.configure(image=None, text="")

    def _login_needed(self) -> bool:
        return self.last_error is not None and self.last_error.status in LOGIN_STATES

    def _on_check_done(self, res: CheckResult) -> None:
        self.stage = "checked"
        self.result = res
        keep = {n for n in ("password", "referer") if n in self._visible}
        status = res.result.status
        if not res.ok:
            self.last_error = res.result
            self._only("banner", *keep)
            self.show_banner(res.result)
            self._reveal_for(status)
            log.info("Check result: %s", status.value)
            self._relayout()
            return

        self.last_error = None
        self._only("banner", "preview", "options", "download", *keep)
        if not self.login.is_none:
            self._visible.add("login")
        self.show_banner(res.result)
        v = res.video
        self.title_label.configure(text=v.title if v else "")
        meta = [x for x in (v.uploader if v else None, human_duration(v.duration) if v else None,
                            v.site if v else None) if x]
        self.meta_label.configure(text="  ·  ".join(meta))
        self.thumb_label.configure(text="")

        values = [q.display for q in res.qualities]
        self.quality_menu.configure(values=values)
        chosen = default_quality(res.qualities, self.settings.default_quality)
        self.quality_menu.set(chosen.display if chosen else values[0])

        if res.playlist and res.playlist.count > 1:
            n = res.playlist.count
            first = "the first video" if res.playlist.pure else "just this video"
            self.playlist_label.configure(
                text=f"This link is a playlist with {n} videos. Download {first}, or all {n} videos?")
            self.playlist_choice.configure(values=["Just this video" if not res.playlist.pure else "First video",
                                                   f"All {n} videos"])
            self.playlist_choice.set("Just this video" if not res.playlist.pure else "First video")
            self._visible.add("playlist")

        self.download_btn.configure(text="Download", state="normal")
        self._refresh_folder_label()
        self._relayout()
        log.info("Check result: ready (%d quality options%s)", len(res.qualities),
                 f", playlist of {res.playlist.count}" if res.playlist else "")

    def _reveal_for(self, status: Status) -> None:
        if status in LOGIN_STATES:
            self._visible.add("login")
            self.login_retry.grid(row=0, column=3, padx=(8, 0))
        else:
            self.login_retry.grid_remove()
        if status is Status.NEEDS_PASSWORD:
            self._visible.add("password")
            self.after(50, self.password_entry.focus_set)
        if status is Status.EMBED_RESTRICTED:
            self._visible.add("referer")
            self.after(50, self.referer_entry.focus_set)

    def _on_thumbnail(self, data: bytes | None) -> None:
        if not data:
            return
        try:
            from PIL import Image
            img = Image.open(io.BytesIO(data))
            img.load()
            img.thumbnail((160, 90))
            self._thumb_image = ctk.CTkImage(light_image=img, dark_image=img, size=img.size)
            self.thumb_label.configure(image=self._thumb_image, text="")
        except Exception as e:  # noqa: BLE001 - a missing thumbnail is cosmetic
            log.debug("Could not show thumbnail: %s", e)

    # ======================================================================================
    # Login source
    # ======================================================================================

    def _login_labels(self) -> list[str]:
        return [s.label for s in self.login_sources] + [CUSTOM_LABEL]

    def _on_login_selected(self, label: str) -> None:
        if label == CUSTOM_LABEL:
            folder = filedialog.askdirectory(parent=self, title="Choose a browser profile folder",
                                             mustexist=True)
            src = browsers.custom_source(folder) if folder else None
            if folder and not src:
                messagebox.showinfo(APP_TITLE, "That folder doesn't contain a saved browser login "
                                               "(no cookies.sqlite file). Choose a Firefox, Zen, LibreWolf "
                                               "or Floorp profile folder.", parent=self)
            if not src:
                self.login_menu.set(self.login.label)
                return
            if src.key not in {s.key for s in self.login_sources}:
                self.login_sources.append(src)
                self.login_menu.configure(values=self._login_labels())
            self.login = src
            self.login_menu.set(src.label)
        else:
            self.login = next((s for s in self.login_sources if s.label == label), browsers.NONE_SOURCE)
        log.info("Login source set to %s", "none" if self.login.is_none else self.login.app_name)
        self._update_login_note()

    def _update_login_note(self) -> None:
        if self.login.note:
            self.login_note.configure(text=self.login.note)
            self.login_note.grid(row=1, column=0, columnspan=4, sticky="ew", pady=(4, 0))
        else:
            self.login_note.grid_remove()

    def set_login_sources(self, sources: list[LoginSource]) -> None:
        self.login_sources = sources
        self.login_menu.configure(values=self._login_labels())

    # ======================================================================================
    # Download
    # ======================================================================================

    def _selected_quality(self):
        if not self.result:
            return None
        shown = self.quality_menu.get()
        return next((q for q in self.result.qualities if q.display == shown), None)

    def _target_folder(self) -> Path:
        return self.folder_override or self.settings.effective_save_folder()

    def _refresh_folder_label(self) -> None:
        self.folder_label.configure(text=elide_middle(str(self._target_folder()), 52))

    def change_folder(self) -> None:
        folder = filedialog.askdirectory(parent=self, title="Save this download to…",
                                         initialdir=str(self._target_folder()), mustexist=True)
        if folder:
            self.folder_override = Path(folder)
            self._refresh_folder_label()

    def start_download(self) -> None:
        if self.stage != "checked" or not self.result or not self.result.ok:
            return
        quality = self._selected_quality()
        if quality is None:
            return
        folder = self._target_folder()
        if not folder.exists() and self.folder_override is None and not self.settings.save_folder:
            try:
                folder.mkdir(parents=True, exist_ok=True)  # a brand-new profile may lack Downloads
            except OSError:
                pass
        problem = settings_mod.validate_folder(folder)
        if problem:
            self.show_banner(ErrorInfo(Status.SAVE_FAILED, f"{problem} Choose another folder with Change."))
            self._relayout()
            return

        res = self.result
        urls = [res.url]
        if res.playlist and "playlist" in self._visible and self.playlist_choice.get().startswith("All"):
            urls = res.playlist.entry_urls
            folder = folder / safe_folder_name(res.playlist.title)
        elif res.playlist and res.playlist.pure:
            urls = res.playlist.entry_urls[:1]

        job = DownloadJob(
            urls=[u for u in urls if u],
            quality=quality,
            folder=folder,
            login=self.login,
            password=self.password_var.get() if "password" in self._visible and self.password_var.get() else None,
            referer=self.referer_var.get().strip() if "referer" in self._visible and self.referer_var.get().strip() else None,
        )
        self._token += 1
        token = self._token
        self.stage = "downloading"
        self._set_inputs_enabled(False)
        keep = {n for n in ("login", "password", "referer", "playlist") if n in self._visible}
        self._only("preview", "options", "progress", *keep)
        self.progress_bar.configure(mode="indeterminate")
        self.progress_bar.start()
        self.progress_text.configure(text="Starting…")
        self.cancel_btn.configure(state="normal", text="Cancel")
        self._relayout()
        log.info("Download started: %d item(s), quality=%s", len(job.urls), quality.key)

        def work():
            result = self.engine.download(job, lambda p: self.events.put(("progress", token, p)))
            self.events.put(("downloaded", token, result))

        self._download_thread = threading.Thread(target=work, name="download", daemon=True)
        self._download_thread.start()

    def cancel_download(self) -> None:
        if self.stage != "downloading":
            return
        self.engine.cancel()
        self.cancel_btn.configure(state="disabled", text="Cancelling…")
        self.progress_text.configure(text="Cancelling and removing partial files…")
        log.info("Cancel requested")

    def _on_progress(self, p: Progress) -> None:
        if self.stage != "downloading":
            return
        prefix = f"Video {p.item} of {p.items} — " if p.items > 1 else ""
        if p.phase == "starting":
            text = prefix + "Starting…"
            indeterminate = True
        elif p.phase == "processing":
            text = prefix + "Finishing up…"
            indeterminate = True
        else:
            bits = []
            if p.percent is not None:
                bits.append(f"{p.percent:.0f}%")
            elif p.downloaded:
                bits.append(f"{human_size(p.downloaded)} so far")
            if p.speed:
                bits.append(f"{human_size(p.speed)}/s")
            if p.eta is not None and p.percent is not None:
                bits.append(_eta_text(p.eta))
            text = prefix + ("  ·  ".join(bits) or "Downloading…")
            indeterminate = p.percent is None
        if self.engine.cancelled:
            return
        if indeterminate:
            if self.progress_bar.cget("mode") != "indeterminate":
                self.progress_bar.configure(mode="indeterminate")
                self.progress_bar.start()
        else:
            if self.progress_bar.cget("mode") != "determinate":
                self.progress_bar.stop()
                self.progress_bar.configure(mode="determinate")
            self.progress_bar.set(max(0.0, min(1.0, (p.percent or 0) / 100)))
        self.progress_text.configure(text=text)

    def _on_download_done(self, r: DownloadResult) -> None:
        self.progress_bar.stop()
        self._set_inputs_enabled(True)
        self._download_thread = None
        keep = {n for n in ("login", "password", "referer", "playlist") if n in self._visible}
        if r.cancelled:
            log.info("Download cancelled")
            self.stage = "checked"
            self._only("banner", "preview", "options", "download", *keep)
            self.show_banner(ErrorInfo(Status.CANCELLED, "Download cancelled. Partial files were removed."))
            self._relayout()
            return
        if not r.files:
            err = r.error or ErrorInfo(Status.UNKNOWN, "Something went wrong.")
            log.info("Download failed: %s", err.status.value)
            self.stage = "checked"
            self.last_error = err
            self._only("banner", "preview", "options", "download", *keep)
            self.show_banner(err)
            self._reveal_for(err.status)
            self.download_btn.configure(text="Try again")
            self._relayout()
            return

        self.stage = "done"
        self.saved_files = r.files
        log.info("Download finished: %d file(s)", len(r.files))
        if len(r.files) == 1:
            text = f"Saved: {r.files[0].name}"
        else:
            text = f"Saved {len(r.files)} videos to {elide_middle(str(r.files[0].parent), 60)}"
        self.done_text.configure(text=text)
        self._only("done")
        if r.failures:
            n = len(r.failures)
            first = r.failures[0][1]
            detail = "\n\n".join(f"{title}\n{info.message}\n{info.detail}" for title, info in r.failures)
            self.show_banner(ErrorInfo(Status.UNKNOWN,
                                       f"{n} video{'s' if n > 1 else ''} couldn't be downloaded. {first.message}",
                                       detail))
            self.banner.configure(fg_color=BANNER_COLORS[Severity.WARN][0])
            self.banner_dot.configure(text_color=BANNER_COLORS[Severity.WARN][1])
        self._relayout()
        self.show_btn.focus_set()

    def show_saved(self) -> None:
        if self.saved_files:
            show_in_folder(self.saved_files[0])

    def download_another(self) -> None:
        if self.stage == "downloading":
            return
        self._token += 1
        self.stage = "empty"
        self.folder_override = None
        self._checked_url = None
        self._clear_result()
        self.password_var.set("")
        self.referer_var.set("")
        self.url_var.set("")
        self._only()
        self._refresh_folder_label()
        self._relayout()
        self.url_entry.focus_set()

    def _set_inputs_enabled(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        for w in (self.url_entry, self.paste_btn, self.check_btn, self.quality_menu, self.login_menu,
                  self.password_entry, self.referer_entry, self.playlist_choice, self.settings_btn):
            try:
                w.configure(state=state)
            except (tk.TclError, ValueError):
                pass

    # ======================================================================================
    # Engine updates
    # ======================================================================================

    def _check_engine_update_async(self) -> None:
        def work():
            try:
                info = updater.check_for_update()
            except Exception as e:  # noqa: BLE001 - launch check is silent on failure
                log.info("Launch update check failed: %s", e)
                return
            if info.available:
                self.events.put(("update_available", None, info))

        threading.Thread(target=work, name="update-check", daemon=True).start()

    def _on_update_available(self, info: updater.UpdateInfo) -> None:
        self._pending_update = info
        self.update_text.configure(text=f"A newer downloader engine is available ({info.latest}).")
        self.update_btn.configure(state="normal", text="Update")
        self.update_btn.grid()
        self._set_visible("update")
        self._relayout()

    def _install_engine_update_async(self) -> None:
        info = self._pending_update
        if not info:
            return
        self.update_btn.configure(state="disabled", text="Updating…")

        def work():
            try:
                updater.install_update(info)
                self.events.put(("update_installed", None, info.latest))
            except Exception as e:  # noqa: BLE001
                log.warning("Engine update failed: %s", e)
                self.events.put(("update_failed", None, str(e)))

        threading.Thread(target=work, name="update-install", daemon=True).start()

    def on_engine_updated(self, version: str) -> None:
        self._pending_update = None
        self.update_text.configure(text=f"Updated to {version} — restart the app to use it.")
        self.update_btn.grid_remove()
        self._set_visible("update")
        self._relayout()

    # ======================================================================================
    # Settings
    # ======================================================================================

    def open_settings(self) -> None:
        if self.stage == "downloading":
            return
        from .ui_settings import SettingsWindow
        existing = getattr(self, "_settings_win", None)
        if existing is not None and existing.winfo_exists():
            existing.focus_set()
            return
        self._settings_win = SettingsWindow(self, self.settings, self.login_sources, on_saved=self._on_settings_saved,
                                            on_engine_updated=self.on_engine_updated,
                                            save_settings=self._save_settings)

    def _on_settings_saved(self, new: Settings) -> None:
        login_changed = new.login_source != self.settings.login_source
        self.settings = new
        if login_changed:
            self.login = browsers.from_key(new.login_source, self.login_sources)
            self.login_menu.set(self.login.label)
            self._update_login_note()
        self._refresh_folder_label()
        if self.result and self.result.ok and self.stage == "checked":
            chosen = default_quality(self.result.qualities, new.default_quality)
            if chosen:
                self.quality_menu.set(chosen.display)
        if not self.login.is_none and self.stage in ("checked", "done"):
            self._visible.add("login")
        self._relayout()

    # ======================================================================================
    # Events, keys, window
    # ======================================================================================

    def _poll(self) -> None:
        last_progress: Progress | None = None
        try:
            while True:
                kind, token, payload = self.events.get_nowait()
                if token is not None and token != self._token:
                    continue  # result of a check/download the user has moved on from
                if kind == "progress":
                    last_progress = payload  # only the newest progress matters
                    continue
                if last_progress is not None:
                    self._on_progress(last_progress)
                    last_progress = None
                if kind == "check":
                    self._on_check_done(payload)
                elif kind == "thumb":
                    self._on_thumbnail(payload)
                elif kind == "downloaded":
                    self._on_download_done(payload)
                elif kind == "update_available":
                    self._on_update_available(payload)
                elif kind == "update_installed":
                    self.on_engine_updated(payload)
                elif kind == "update_failed":
                    self.update_text.configure(text="Couldn't update the engine. Try again later.")
                    self.update_btn.configure(state="normal", text="Retry")
        except queue.Empty:
            pass
        except Exception:  # noqa: BLE001 - never let the poll loop die
            log.exception("Error while handling a background event")
        if last_progress is not None:
            self._on_progress(last_progress)
        self._poll_id = self.after(POLL_MS, self._poll)

    def _bind_keys(self) -> None:
        self.bind("<Return>", self._on_enter)
        self.bind("<KP_Enter>", self._on_enter)
        self.bind("<Escape>", self._on_escape)
        for seq in ("<Control-v>", "<Control-V>"):
            self.bind(seq, self._on_ctrl_v)
            self.url_entry.bind(seq, self._paste_into_url)
        self.url_entry.bind("<<Paste>>", self._paste_into_url)

    def _on_enter(self, _event=None):
        if self.stage == "checked" and self.result and self.result.ok:
            self.start_download()
        elif self.stage in ("empty", "checked"):
            self.start_check()
        return "break"

    def _on_escape(self, _event=None):
        if self.stage == "downloading":
            self.cancel_download()
        elif self.stage == "checking":
            self._token += 1  # abandon the running check
            self.stage = "empty"
            self._checked_url = None
            self._only()
            self._relayout()
            log.info("Check abandoned")
        return "break"

    def _on_ctrl_v(self, event=None):
        focus = self.focus_get()
        if isinstance(focus, tk.Entry) and focus is not self.url_entry._entry:
            return None  # normal paste into the password/page fields
        return self._paste_into_url(event)

    def _paste_into_url(self, _event=None):
        if self.stage != "downloading":
            self.paste_url()
        return "break"

    def _restore_geometry(self) -> None:
        geo = self.settings.window_geometry
        default = "620x260"
        if not geo:
            self.geometry(default)
            return
        try:
            size, _, pos = geo.partition("+")
            w, h = (int(x) for x in size.split("x"))
            w = max(w, MIN_WIDTH)
            x_str, _, y_str = pos.partition("+")
            if x_str and y_str:
                x, y = int(x_str), int(y_str)
                if 0 <= x < self.winfo_screenwidth() - 100 and 0 <= y < self.winfo_screenheight() - 100:
                    tk.Tk.geometry(self, f"{w}x{h}+{x}+{y}")
                    return
            tk.Tk.geometry(self, f"{w}x{h}")
        except (ValueError, tk.TclError):
            self.geometry(default)

    def _remember_geometry(self) -> None:
        try:
            self.settings.window_geometry = tk.Tk.geometry(self)
            self._save_settings(self.settings)
        except Exception as e:  # noqa: BLE001
            log.warning("Could not save window size: %s", e)

    def _on_close(self) -> None:
        if self.stage == "downloading":
            if not messagebox.askyesno(APP_TITLE, "A download is in progress. Cancel it and quit?", parent=self):
                return
            self.engine.cancel()
            t = self._download_thread
            if t is not None:
                t.join(timeout=15)  # let the engine delete partial files
        self._remember_geometry()
        self.destroy()


    def destroy(self) -> None:
        for after_id in (getattr(self, "_poll_id", None), self._debounce):
            if after_id:
                try:
                    self.after_cancel(after_id)
                except tk.TclError:
                    pass
        super().destroy()


def _eta_text(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return "less than a minute left" if s > 5 else "almost done"
    m = round(s / 60)
    if m < 60:
        return f"about {m} min left"
    h, m = divmod(m, 60)
    return f"about {h} h {m} min left"
