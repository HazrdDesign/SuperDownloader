"""Main window: paste a link, see whether it works, pick a quality and format, download.

Downloads go through a queue (app/jobs.py). A download started from the form is shown in
the form (progress, then "Saved"); anything added while something is running, or pasted
as a batch of links, waits in the Queue panel. Finished downloads go to History.
"""

from __future__ import annotations

import io
import logging
import queue
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox
from typing import Callable
from urllib.parse import urlparse

import customtkinter as ctk

from . import browsers, history as history_mod, settings as settings_mod, theme, updater
from .browsers import LoginSource
from .engine import (
    FORMATS_BY_KEY,
    CheckRequest,
    CheckResult,
    DownloadJob,
    DownloadResult,
    Engine,
    OutputFormat,
    Progress,
    Quality,
    SubtitleChoice,
    default_quality,
    extract_urls,
    fetch_thumbnail,
    format_by_key,
    format_timecode,
    human_duration,
    human_size,
    normalize_url,
    parse_timecode,
    project_parts,
    project_prefix,
    safe_folder_name,
)
from .errors import FIREFOX_FAMILY_HINT, ErrorInfo, Severity, Status
from .history import History, HistoryEntry
from .jobs import JobQueue, QueuedJob
from .settings import Settings
from .ui_common import (
    BANNER_COLORS,
    BANNER_DOT,
    MUTED,
    PAD,
    Tooltip,
    elide_middle,
    link_label,
    notify_done,
    set_window_icon,
    show_in_folder,
)

log = logging.getLogger(__name__)

APP_TITLE = "Super Downloader"
DEBOUNCE_MS = 600
POLL_MS = 50
MIN_WIDTH = 540
HISTORY_ROWS_SHOWN = 50
NO_SUBTITLES = "No subtitles"

LOGIN_STATES = {Status.NEEDS_LOGIN, Status.COOKIES_LOCKED, Status.COOKIES_UNREADABLE, Status.COOKIES_MISSING}
LOGIN_HOSTS = ("vimeo.com",)  # sites where the login option is offered up front

# Sections in the order they appear on screen.
SECTIONS = ("header", "url", "spinner", "banner", "login", "password", "referer", "preview", "playlist",
            "options", "more", "download", "progress", "done", "queue", "update", "history")

try:  # drag and drop of links onto the window (optional; the app works without it)
    from tkinterdnd2 import DND_TEXT, TkinterDnD
    _DnDBase = TkinterDnD.DnDWrapper
except Exception:  # noqa: BLE001
    TkinterDnD = None
    DND_TEXT = None
    _DnDBase = object


def _is_login_host(url: str | None) -> bool:
    host = (urlparse(url or "").hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in LOGIN_HOSTS)


def _site(url: str | None) -> str:
    """"vimeo.com" for https://player.vimeo.com/...: the key for remembering a site's login."""
    host = (urlparse(url or "").hostname or "").lower()
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) > 2 else host


class MainWindow(ctk.CTk, _DnDBase):
    def __init__(self, settings: Settings, engine: Engine | None = None, *,
                 login_sources: list[LoginSource] | None = None, check_updates: bool | None = None,
                 save_settings: Callable[[Settings], None] | None = None, warm_up_engine: bool = True,
                 history: History | None = None):
        super().__init__()
        # Background threads run CPU-heavy yt-dlp code. A short GIL switch interval keeps
        # Tk's many small Python callbacks from queueing behind it (default is 5 ms).
        sys.setswitchinterval(0.001)
        self.settings = settings
        self.engine = engine or Engine()
        engine_factory = (lambda: engine) if engine is not None else Engine
        self._save_settings = save_settings or settings_mod.save
        self.events: queue.Queue = queue.Queue()
        self.queue = JobQueue(engine_factory, lambda kind, payload: self.events.put((kind, None, payload)))
        self.history = history if history is not None else History()
        self.stage = "empty"  # empty, checking, checked, downloading, done
        self.result: CheckResult | None = None
        self.last_error: ErrorInfo | None = None
        self.saved_files: list[Path] = []
        self.folder_override: Path | None = None
        self.focus_job: QueuedJob | None = None       # the download shown in the form
        self._token = 0
        self._debounce: str | None = None
        self._checked_url: str | None = None
        self._visible: set[str] = {"header", "url"}
        self._thumb_image: ctk.CTkImage | None = None
        self._blank_thumb: ctk.CTkImage | None = None
        self._redo: HistoryEntry | None = None         # options to restore after a history re-check
        self._last_clipboard: str | None = None
        self._queue_rows: dict[int, dict] = {}
        self._more_open = False
        self.banner_status: Status | None = None

        self.login_sources = login_sources if login_sources is not None else browsers.all_sources()
        self._unreadable_logins: set[str] = set()  # browsers whose login couldn't be read this session

        self.title(APP_TITLE)
        set_window_icon(self)
        self.minsize(MIN_WIDTH, 200)
        self._restore_geometry()
        self._build()
        self._bind_keys()
        self._enable_drag_and_drop()
        self._refresh_history()
        self._relayout()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._poll_id = self.after(POLL_MS, self._poll)
        self.after(100, self.url_entry.focus_set)
        self.after(200, self._fit_height)
        # Only links copied while the app is open are picked up, not whatever old link
        # happens to be on the clipboard when it starts.
        try:
            self._last_clipboard = self.clipboard_get().strip()
        except tk.TclError:
            self._last_clipboard = ""

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
        self.sections: dict[str, ctk.CTkFrame] = {}
        secondary = theme.SECONDARY_BUTTON
        self._secondary = secondary

        # -- header -------------------------------------------------------------------------
        header = self._section("header")
        header.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(header, text=APP_TITLE, font=ctk.CTkFont(size=18, weight="bold")).grid(
            row=0, column=0, sticky="w")
        self.settings_btn = ctk.CTkButton(header, text="⚙", width=36, height=32, font=ctk.CTkFont(size=18),
                                          fg_color="transparent", hover_color=theme.SURFACE_2,
                                          text_color=theme.TEXT, command=self.open_settings)
        self.settings_btn.grid(row=0, column=1, sticky="e")
        Tooltip(self.settings_btn, "Settings")

        # -- URL ----------------------------------------------------------------------------
        url = self._section("url")
        url.grid_columnconfigure(0, weight=1)
        self.url_var = tk.StringVar()
        self.url_entry = ctk.CTkEntry(url, textvariable=self.url_var, height=38,
                                      placeholder_text="Paste a video link (or several) here")
        self.url_entry.grid(row=0, column=0, sticky="ew")
        self.paste_btn = ctk.CTkButton(url, text="Paste", width=70, height=38, command=self.paste_url, **secondary)
        self.paste_btn.grid(row=0, column=1, padx=(8, 0))
        self.check_btn = ctk.CTkButton(url, text="Check", width=70, height=38, command=self.start_check,
                                       **secondary)
        self.check_btn.grid(row=0, column=2, padx=(8, 0))
        self.url_var.trace_add("write", self._on_url_changed)

        # -- spinner ------------------------------------------------------------------------
        spinner = self._section("spinner")
        spinner.grid_columnconfigure(1, weight=1)
        self.spinner_label = ctk.CTkLabel(spinner, text="Checking link…", text_color=MUTED)
        self.spinner_label.grid(row=0, column=0, padx=(0, 10))
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

        # -- login: one checkbox, no browser choice here (that's in Settings) ------------------
        login = self._section("login")
        login.grid_columnconfigure(0, weight=1)
        self.use_login_var = tk.BooleanVar(value=False)
        self.login_check = ctk.CTkCheckBox(login, text="", variable=self.use_login_var)
        self.login_check.grid(row=0, column=0, sticky="w")
        self.login_retry = ctk.CTkButton(login, text="Retry", width=70, command=self.retry)
        self.login_note = ctk.CTkLabel(login, text="", text_color=MUTED, justify="left", anchor="w", wraplength=440)
        Tooltip(self.login_check, browsers.LOGIN_TOOLTIP)
        self._update_login_row()

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
        prev = self._section("preview", corner_radius=8, fg_color=theme.SURFACE)
        prev.grid_columnconfigure(1, weight=1)
        self.thumb_label = ctk.CTkLabel(prev, text="", width=160, height=90, fg_color=theme.SURFACE_2,
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

        # -- options: quality, format, save to --------------------------------------------------
        opt = self._section("options")
        opt.grid_columnconfigure(1, weight=1)
        self.quality_label = ctk.CTkLabel(opt, text="Quality")
        self.quality_label.grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.quality_menu = ctk.CTkOptionMenu(opt, values=["—"], dynamic_resizing=False)
        self.quality_menu.grid(row=0, column=1, columnspan=2, sticky="ew")
        ctk.CTkLabel(opt, text="Format").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=(8, 0))
        self.format_menu = ctk.CTkOptionMenu(opt, values=["—"], dynamic_resizing=False,
                                             command=lambda _v: self._on_format_changed())
        self.format_menu.grid(row=1, column=1, columnspan=2, sticky="ew", pady=(8, 0))
        ctk.CTkLabel(opt, text="Save to").grid(row=2, column=0, sticky="w", padx=(0, 8), pady=(8, 0))
        self.folder_label = ctk.CTkLabel(opt, text="", anchor="w", text_color=MUTED)
        self.folder_label.grid(row=2, column=1, sticky="ew", pady=(8, 0))
        ctk.CTkButton(opt, text="Change", width=70, command=self.change_folder, **secondary).grid(
            row=2, column=2, padx=(8, 0), pady=(8, 0))
        self.more_link = link_label(opt, "More options ▸", self.toggle_more)
        self.more_link.grid(row=3, column=0, columnspan=3, sticky="w", pady=(6, 0))

        # -- more options: clip, subtitles, project ---------------------------------------------
        more = self._section("more", corner_radius=8, fg_color=theme.SURFACE)
        more.grid_columnconfigure(1, weight=1)
        self.clip_var = tk.BooleanVar(value=False)
        ctk.CTkCheckBox(more, text="Only part of the video", variable=self.clip_var,
                        command=self._on_clip_toggled).grid(row=0, column=0, columnspan=2, sticky="w", padx=12,
                                                            pady=(12, 0))
        clip_row = ctk.CTkFrame(more, fg_color="transparent")
        self.clip_row = clip_row
        ctk.CTkLabel(clip_row, text="From").pack(side="left")
        self.clip_start = ctk.CTkEntry(clip_row, width=80, placeholder_text="0:00")
        self.clip_start.pack(side="left", padx=6)
        ctk.CTkLabel(clip_row, text="to").pack(side="left")
        self.clip_end = ctk.CTkEntry(clip_row, width=80, placeholder_text="1:00")
        self.clip_end.pack(side="left", padx=6)
        ctk.CTkLabel(clip_row, text="(minutes:seconds)", text_color=MUTED).pack(side="left", padx=4)
        self.subs_label = ctk.CTkLabel(more, text="Subtitles")
        self.subs_label.grid(row=2, column=0, sticky="w", padx=(12, 8), pady=(10, 0))
        self.subs_menu = ctk.CTkOptionMenu(more, values=[NO_SUBTITLES], dynamic_resizing=False)
        self.subs_menu.grid(row=2, column=1, sticky="ew", padx=(0, 12), pady=(10, 0))
        ctk.CTkLabel(more, text="Project").grid(row=3, column=0, sticky="w", padx=(12, 8), pady=(10, 12))
        self.project_box = ctk.CTkComboBox(more, values=self.settings.recent_projects or [""],
                                           command=lambda _v: self._refresh_folder_label())
        self.project_box.set("")
        self.project_box.grid(row=3, column=1, sticky="ew", padx=(0, 12), pady=(10, 12))
        self.project_box._entry.bind("<KeyRelease>", lambda _e: self._refresh_folder_label(), add="+")
        Tooltip(self.project_box, "Optional. Type \"Client / Project\": files go into that folder and are "
                                  "named Client_Project_date_Title.")

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
        self.cancel_btn = ctk.CTkButton(prog, text="Cancel", width=80, command=self.cancel_download, **secondary)
        self.cancel_btn.grid(row=0, column=1, padx=(10, 0))
        self.progress_text = ctk.CTkLabel(prog, text="", text_color=MUTED, anchor="w")
        self.progress_text.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(4, 0))

        # -- done ---------------------------------------------------------------------------
        done = self._section("done", corner_radius=8, fg_color=theme.OK_BG)
        done.grid_columnconfigure(0, weight=1)
        self.done_text = ctk.CTkLabel(done, text="", justify="left", anchor="w", wraplength=460,
                                      font=ctk.CTkFont(weight="bold"))
        self.done_text.grid(row=0, column=0, columnspan=2, sticky="ew", padx=12, pady=(10, 6))
        self.show_btn = ctk.CTkButton(done, text="Show in folder", command=self.show_saved)
        self.show_btn.grid(row=1, column=0, sticky="w", padx=12, pady=(0, 12))
        ctk.CTkButton(done, text="Download another", command=self.download_another, **secondary).grid(
            row=1, column=1, sticky="e", padx=12, pady=(0, 12))

        # -- queue --------------------------------------------------------------------------
        q = self._section("queue")
        q.grid_columnconfigure(0, weight=1)
        self.queue_title = ctk.CTkLabel(q, text="Queue", font=ctk.CTkFont(weight="bold"), anchor="w")
        self.queue_title.grid(row=0, column=0, sticky="w")
        self.queue_list = ctk.CTkFrame(q, fg_color=theme.SURFACE, corner_radius=8)
        self.queue_list.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        self.queue_list.grid_columnconfigure(0, weight=1)

        # -- engine update notice -----------------------------------------------------------
        upd = self._section("update")
        upd.grid_columnconfigure(0, weight=1)
        self.update_text = ctk.CTkLabel(upd, text="", text_color=MUTED, anchor="w", justify="left", wraplength=380)
        self.update_text.grid(row=0, column=0, sticky="ew")
        self.update_btn = ctk.CTkButton(upd, text="Update", width=80, command=self._install_engine_update_async)
        self.update_btn.grid(row=0, column=1, padx=(8, 0))
        self._pending_update: updater.UpdateInfo | None = None

        # -- history ------------------------------------------------------------------------
        h = self._section("history")
        h.grid_columnconfigure(0, weight=1)
        head = ctk.CTkFrame(h, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew")
        head.grid_columnconfigure(0, weight=1)
        self.history_title = ctk.CTkLabel(head, text="History", font=ctk.CTkFont(weight="bold"), anchor="w")
        self.history_title.grid(row=0, column=0, sticky="w")
        link_label(head, "Clear", self.clear_history).grid(row=0, column=1, sticky="e")
        self.history_list = ctk.CTkScrollableFrame(h, height=170, fg_color=theme.SURFACE, corner_radius=8)
        self.history_list.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        self.history_list.grid_columnconfigure(0, weight=1)

        self._refresh_folder_label()

    def _section(self, name: str, **kw) -> ctk.CTkFrame:
        kw.setdefault("fg_color", "transparent")
        frame = ctk.CTkFrame(self.body, **kw)
        self.sections[name] = frame
        return frame

    def _set_visible(self, *names: str, visible: bool = True) -> None:
        for n in names:
            (self._visible.add if visible else self._visible.discard)(n)

    def _only(self, *names: str) -> None:
        """Show exactly the header, URL row and the given sections (plus the always-on panels)."""
        self._visible = {"header", "url", *names}
        if self._pending_update is not None or self.update_text.cget("text"):
            self._visible.add("update")

    def _relayout(self) -> None:
        if any(j is not self.focus_job for j in self.queue.active()):
            self._visible.add("queue")
        else:
            self._visible.discard("queue")
        if self.history.entries:
            self._visible.add("history")
        else:
            self._visible.discard("history")
        if not self._more_open or "options" not in self._visible:
            self._visible.discard("more")
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

    def _warn(self, message: str) -> None:
        self.show_banner(ErrorInfo(Status.UNKNOWN, message))
        self.banner.configure(fg_color=theme.WARN_BG)
        self.banner_dot.configure(text_color=theme.WARN)
        self._relayout()

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
        self.accept_text(text)

    def accept_text(self, text: str) -> None:
        """Handle pasted or dropped text: one link is checked, several go straight to the queue."""
        text = (text or "").strip()
        urls = extract_urls(text)
        if len(urls) > 1:
            self.queue_links(urls)
            return
        self.url_var.set(urls[0] if urls else text)
        self.url_entry.icursor("end")
        self.url_entry.focus_set()
        if text:
            self.start_check()

    def _on_url_changed(self, *_):
        if self._debounce:
            self.after_cancel(self._debounce)
            self._debounce = None
        text = self.url_var.get()
        url = normalize_url(text)
        if url != self._checked_url and self.stage in ("checked", "checking", "done", "downloading"):
            self._detach_focus_job()
            self._token += 1  # any running check is now stale
            self._clear_result()
            self.stage = "empty"
            self._only()
            self._relayout()
        if url and url != self._checked_url:
            self._debounce = self.after(DEBOUNCE_MS, self.start_check)

    def retry(self) -> None:
        self.start_check()

    def login_candidates(self, url: str | None = None) -> list[LoginSource]:
        """The browser logins to try for ``url``, in order (empty if logins are off or none found)."""
        key = self.settings.login_source
        if key == browsers.NONE_KEY:
            return []
        if key and key != browsers.AUTO_KEY:
            src = browsers.from_key(key, self.login_sources)
            return [] if src.is_none else [src]
        url = url if url is not None else (self._checked_url or normalize_url(self.url_var.get()))
        return browsers.candidates(self.login_sources, self.settings.site_logins.get(_site(url)),
                                   demote=self._unreadable_logins)

    def _check_request(self, text: str) -> CheckRequest:
        cands = self.login_candidates(normalize_url(text))
        if not cands:
            first, rest = browsers.NONE_SOURCE, ()
        elif self.use_login_var.get():
            # Try each browser, then (if none can be read) without a login.
            first, rest = cands[0], (*cands[1:], browsers.NONE_SOURCE)
        else:
            # Try without a login; if the site asks for one, try each browser.
            first, rest = browsers.NONE_SOURCE, tuple(cands)
        return CheckRequest(
            url=text,
            login=first,
            password=self.password_var.get() if "password" in self._visible and self.password_var.get() else None,
            referer=self.referer_var.get().strip() if "referer" in self._visible and self.referer_var.get().strip() else None,
            fallback_logins=rest,
        )

    def start_check(self) -> None:
        if self._debounce:
            self.after_cancel(self._debounce)
            self._debounce = None
        text = self.url_var.get().strip()
        if not text:
            self.url_entry.focus_set()
            return
        if self.stage == "downloading":
            self._detach_focus_job()
        self._token += 1
        token = self._token
        new_url = normalize_url(text)
        if new_url != self._checked_url and _is_login_host(new_url) and self.login_candidates(new_url):
            self.use_login_var.set(True)  # Vimeo: use the browser login up front
        self._checked_url = new_url
        req = self._check_request(text)
        keep = {n for n in ("password", "referer") if n in self._visible}
        if _is_login_host(new_url) or "login" in self._visible:
            keep.add("login")
        self._clear_result()
        self.stage = "checking"
        self.spinner_label.configure(text="Checking link…")
        self._only("spinner", *keep)
        self._update_login_row()
        self._relayout()
        host = urlparse(self._checked_url or "").hostname or "?"
        log.info("Check started for %s", host)

        def on_attempt(src: LoginSource) -> None:
            self.events.put(("check_attempt", token, src))

        def work():
            res = self.engine.check_link(req, on_attempt=on_attempt)
            self.events.put(("check", token, res))
            if res.ok and res.video and res.video.thumbnail:
                self.events.put(("thumb", token, fetch_thumbnail(res.video.thumbnail)))

        threading.Thread(target=work, name="check", daemon=True).start()

    def _clear_result(self) -> None:
        self.result = None
        self.last_error = None
        self.saved_files = []
        self._set_thumbnail(None)

    def _login_needed(self) -> bool:
        return self.last_error is not None and self.last_error.status in LOGIN_STATES

    def _on_check_done(self, res: CheckResult) -> None:
        self.stage = "checked"
        self.result = res
        keep = {n for n in ("password", "referer") if n in self._visible}
        self._learn_from_logins(res)
        if res.used_login:
            self.use_login_var.set(True)
        if res.used_login or _is_login_host(res.url or self._checked_url) or self.use_login_var.get():
            keep.add("login")
        status = res.result.status
        if not res.ok:
            self.last_error = res.result
            self._only("banner", *keep)
            self.show_banner(res.result)
            self._reveal_for(status)
            log.info("Check result: %s", status.value)
            self._update_login_row()
            self._relayout()
            return

        self.last_error = None
        self._only("banner", "preview", "options", "download", *keep)
        if self._more_open:
            self._visible.add("more")
        self.login_retry.grid_remove()
        self.show_banner(res.result)
        v = res.video
        self.title_label.configure(text=v.title if v else "")
        meta = [x for x in (v.uploader if v else None, human_duration(v.duration) if v else None,
                            v.site if v else None) if x]
        self.meta_label.configure(text="  ·  ".join(meta))

        if res.qualities:
            values = [q.display for q in res.qualities]
            self.quality_menu.configure(values=values)
            chosen = default_quality(res.qualities, self.settings.default_quality)
            self.quality_menu.set(chosen.display if chosen else values[0])
        formats = res.formats
        self.format_menu.configure(values=[f.label for f in formats])
        preferred = format_by_key(self.settings.default_format)
        self.format_menu.set(preferred.label if preferred in formats else formats[0].label)

        subs = [NO_SUBTITLES] + [s.label for s in res.subtitles]
        self.subs_menu.configure(values=subs)
        self.subs_menu.set(NO_SUBTITLES)
        self.clip_end.configure(placeholder_text=human_duration(v.duration) if v and v.duration else "1:00")

        if res.playlist and res.playlist.count > 1:
            n = res.playlist.count
            first = "the first video" if res.playlist.pure else "just this video"
            self.playlist_label.configure(
                text=f"This link is a playlist with {n} videos. Download {first}, or all {n} videos?")
            self.playlist_choice.configure(values=["Just this video" if not res.playlist.pure else "First video",
                                                   f"All {n} videos"])
            self.playlist_choice.set("Just this video" if not res.playlist.pure else "First video")
            self._visible.add("playlist")

        if self._redo is not None:
            self._apply_redo(res)
        self._on_format_changed(relayout=False)
        self.download_btn.configure(text="Download", state="normal")
        self._update_login_row()
        self._refresh_folder_label()
        self._relayout()
        log.info("Check result: ready (%d quality options%s%s)", len(res.qualities),
                 f", playlist of {res.playlist.count}" if res.playlist else "",
                 f", with the login from {res.login.app_name}" if res.used_login else "")

    def _learn_from_logins(self, res: CheckResult) -> None:
        """Remember which browser worked for this site, and which ones can't be read."""
        for key, status in res.failed_logins:
            if status is Status.COOKIES_UNREADABLE:
                self._unreadable_logins.add(key)  # try it last from now on
        if res.ok and res.used_login and self.settings.login_source == browsers.AUTO_KEY:
            self._unreadable_logins.discard(res.login.key)
            if settings_mod.remember_site_login(self.settings, _site(res.url or self._checked_url), res.login.key):
                try:
                    self._save_settings(self.settings)
                except OSError as e:
                    log.warning("Could not save the login used for this site: %s", e)

    def _reveal_for(self, status: Status) -> None:
        if status in LOGIN_STATES:
            self._visible.add("login")
            self.login_retry.grid(row=0, column=1, padx=(8, 0))
        else:
            self.login_retry.grid_remove()
        if status is Status.NEEDS_PASSWORD:
            self._visible.add("password")
            self.after(50, self.password_entry.focus_set)
        if status is Status.EMBED_RESTRICTED:
            self._visible.add("referer")
            self.after(50, self.referer_entry.focus_set)

    def _set_thumbnail(self, image: ctk.CTkImage | None) -> None:
        """Show ``image`` (or the blank placeholder) in the preview.

        The label must be pointed at the new image *before* the old one is released: a
        released CTkImage deletes its Tk image, and a label still using it then fails every
        later configure() with 'image "pyimageN" doesn't exist'.
        """
        if self._blank_thumb is None:
            from PIL import Image
            blank = Image.new("RGBA", (1, 1), (0, 0, 0, 0))
            self._blank_thumb = ctk.CTkImage(light_image=blank, dark_image=blank, size=(1, 1))
        self.thumb_label.configure(image=image or self._blank_thumb, text="")
        self._thumb_image = image  # the previous image is released only now

    def _on_thumbnail(self, data: bytes | None) -> None:
        if not data:
            return
        try:
            from PIL import Image
            img = Image.open(io.BytesIO(data))
            img.load()
            img.thumbnail((160, 90))
            self._set_thumbnail(ctk.CTkImage(light_image=img, dark_image=img, size=img.size))
        except Exception as e:  # noqa: BLE001 - a missing thumbnail is cosmetic
            log.debug("Could not show thumbnail: %s", e)

    # ======================================================================================
    # Login (a single checkbox; the browser itself is chosen automatically or in Settings)
    # ======================================================================================

    def _update_login_row(self) -> None:
        cands = self.login_candidates()
        note = ""
        if not cands:
            self.login_check.grid_remove()
            if self.settings.login_source == browsers.NONE_KEY:
                note = "Browser logins are turned off. To use them, open Settings → Browser for logins."
            else:
                hint = f" ({FIREFOX_FAMILY_HINT})" if sys.platform == "win32" else ""
                note = (f"To download private or password-protected videos, log into the site in your "
                        f"browser{hint}, then click Retry.")
        else:
            names = list(dict.fromkeys(s.app_name for s in cands if s.app_name))
            who = names[0] if len(names) == 1 else "my browser"
            self.login_check.configure(text=f"Log in with {who} (for private or password-protected videos)")
            self.login_check.grid()
            res = self.result
            if res is not None and res.ok and res.used_login:
                note = f"Using your login from {res.login.app_name}."
            elif len(cands) == 1 and cands[0].note:
                note = cands[0].note
        if note:
            self.login_note.configure(text=note)
            self.login_note.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        else:
            self.login_note.grid_remove()

    # ======================================================================================
    # Options
    # ======================================================================================

    def _selected_quality(self) -> Quality | None:
        if not self.result or not self.result.qualities:
            return None
        shown = self.quality_menu.get()
        return next((q for q in self.result.qualities if q.display == shown), self.result.qualities[0])

    def _selected_format(self) -> OutputFormat:
        shown = self.format_menu.get()
        return next((f for f in FORMATS_BY_KEY.values() if f.label == shown), format_by_key(None))

    def _selected_subtitles(self) -> SubtitleChoice | None:
        if not self.result or self._selected_format().kind != "video":
            return None
        shown = self.subs_menu.get()
        return next((s for s in self.result.subtitles if s.label == shown), None)

    def _on_format_changed(self, relayout: bool = True) -> None:
        fmt = self._selected_format()
        show_quality = fmt.uses_quality and bool(self.result and self.result.qualities)
        for w in (self.quality_label, self.quality_menu):
            w.grid() if show_quality else w.grid_remove()
        has_subs = bool(self.result and self.result.subtitles) and fmt.kind == "video"
        for w in (self.subs_label, self.subs_menu):
            w.grid() if has_subs else w.grid_remove()
        if relayout:
            self._relayout()

    def toggle_more(self) -> None:
        self._more_open = not self._more_open
        self.more_link.configure(text="Fewer options ▾" if self._more_open else "More options ▸")
        self._set_visible("more", visible=self._more_open)
        self._on_clip_toggled(relayout=False)
        self._relayout()

    def _on_clip_toggled(self, relayout: bool = True) -> None:
        if self.clip_var.get():
            self.clip_row.grid(row=1, column=0, columnspan=2, sticky="w", padx=12, pady=(6, 0))
        else:
            self.clip_row.grid_remove()
        if relayout:
            self._fit_height()

    def _project_text(self) -> str:
        return self.project_box.get().strip()

    def _target_folder(self) -> Path:
        base = self.folder_override or self.settings.effective_save_folder()
        return base.joinpath(*project_parts(self._project_text()))

    def _refresh_folder_label(self) -> None:
        self.folder_label.configure(text=elide_middle(str(self._target_folder()), 52))

    def change_folder(self) -> None:
        from tkinter import filedialog
        start = self.folder_override or self.settings.effective_save_folder()
        folder = filedialog.askdirectory(parent=self, title="Save this download to…", initialdir=str(start),
                                         mustexist=True)
        if folder:
            self.folder_override = Path(folder)
            self._refresh_folder_label()

    def _clip_range(self) -> tuple[float, float] | None | str:
        """The chosen clip, None for the whole video, or an error message."""
        if not self.clip_var.get():
            return None
        start_txt, end_txt = self.clip_start.get().strip(), self.clip_end.get().strip()
        start = parse_timecode(start_txt) if start_txt else 0.0
        duration = self.result.video.duration if self.result and self.result.video else None
        end = parse_timecode(end_txt) if end_txt else duration
        if start is None or (end_txt and end is None):
            return "Enter the clip times as minutes:seconds, for example 1:30."
        if end is None:
            return "Enter where the clip should end."
        if end <= start:
            return "The clip must end after it starts."
        if duration and start >= duration:
            return f"The video is only {human_duration(duration)} long."
        return (start, min(end, duration) if duration else end)

    # ======================================================================================
    # Download (through the queue)
    # ======================================================================================

    def _ensure_folder(self, folder: Path) -> str | None:
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        return settings_mod.validate_folder(folder)

    def start_download(self) -> None:
        if self.stage != "checked" or not self.result or not self.result.ok:
            return
        res = self.result
        fmt = self._selected_format()
        quality = self._selected_quality() if fmt.uses_quality else None
        clip = self._clip_range()
        if isinstance(clip, str):
            self._warn(clip)
            return
        project = self._project_text()
        folder = self._target_folder()
        problem = self._ensure_folder(folder)
        if problem:
            self.show_banner(ErrorInfo(Status.SAVE_FAILED, f"{problem} Choose another folder with Change."))
            self._relayout()
            return

        urls = [res.url]
        playlist_all = False
        if res.playlist and "playlist" in self._visible and self.playlist_choice.get().startswith("All"):
            urls = res.playlist.entry_urls
            folder = folder / safe_folder_name(res.playlist.title)
            playlist_all = True
        elif res.playlist and res.playlist.pure:
            urls = res.playlist.entry_urls[:1]

        use_login = res.used_login  # the login that worked during the check (if one was needed)
        subs = self._selected_subtitles()
        job = DownloadJob(
            urls=[u for u in urls if u],
            quality=quality,
            folder=folder,
            login=res.login,
            password=self.password_var.get() if "password" in self._visible and self.password_var.get() else None,
            referer=self.referer_var.get().strip() if "referer" in self._visible and self.referer_var.get().strip() else None,
            fmt=fmt,
            clip=clip,
            subtitles=subs,
            name_prefix=project_prefix(project),
        )
        title = res.video.title if res.video else res.url
        meta = {
            "url": res.url, "title": title, "site": res.video.site if res.video else "",
            "quality_key": quality.key if quality else "best", "format_key": fmt.key, "folder": str(folder),
            "used_login": use_login, "referer": job.referer or "", "project": project,
            "clip": list(clip) if clip else [], "subtitles": subs.lang if subs else "",
            "subtitles_auto": bool(subs and subs.auto), "playlist_all": playlist_all,
        }
        if project:
            settings_mod.remember_project(self.settings, project)
            self.project_box.configure(values=self.settings.recent_projects)
            try:
                self._save_settings(self.settings)
            except OSError as e:
                log.warning("Could not save recent projects: %s", e)

        was_busy = self.queue.busy
        qj = self.queue.add(job, title, meta)
        log.info("Download requested: %d item(s), quality=%s, format=%s%s%s", len(job.urls),
                 quality.key if quality else "-", fmt.key, ", clip" if clip else "", ", subtitles" if subs else "")
        if was_busy:
            # Something is already downloading: this one waits in the queue; the form is free.
            self.download_another()
            self.show_banner(ErrorInfo(Status.READY, f"Added to the queue: {elide_middle(title, 60)}"))
            self._relayout()
            return

        self.focus_job = qj
        self._token += 1
        self.stage = "downloading"
        keep = {n for n in ("login", "password", "referer", "playlist") if n in self._visible}
        self._only("preview", "options", "progress", *keep)
        self._set_options_enabled(False)
        self.progress_bar.configure(mode="indeterminate")
        self.progress_bar.start()
        self.progress_text.configure(text="Starting…")
        self.cancel_btn.configure(state="normal", text="Cancel")
        self._relayout()

    def queue_links(self, urls: list[str]) -> None:
        """Add several pasted/dropped links to the queue with the default quality and format."""
        folder = self._target_folder()
        problem = self._ensure_folder(folder)
        if problem:
            self.show_banner(ErrorInfo(Status.SAVE_FAILED, f"{problem} Choose another folder in Settings."))
            self._relayout()
            return
        fmt = format_by_key(self.settings.default_format)
        dq = self.settings.default_quality
        quality = Quality(f"h{dq}", f"{dq}p", height=int(dq)) if dq.isdigit() else Quality("best", "Best available")
        project = self._project_text()
        for url in urls:
            cands = self.login_candidates(url) if _is_login_host(url) else []
            login = cands[0] if cands else browsers.NONE_SOURCE
            job = DownloadJob(urls=[url], quality=quality if fmt.uses_quality else None, folder=folder,
                              login=login, fmt=fmt, name_prefix=project_prefix(project))
            meta = {"url": url, "title": url, "quality_key": quality.key, "format_key": fmt.key,
                    "folder": str(folder), "used_login": not login.is_none, "project": project}
            self.queue.add(job, url, meta)
        log.info("Queued %d links from a batch paste", len(urls))
        self._detach_focus_job()
        self._clear_result()
        self.stage = "empty"
        self._checked_url = None
        self.url_var.set("")
        self._only("banner")
        self.show_banner(ErrorInfo(Status.READY, f"Added {len(urls)} links to the queue. They download one "
                                                 "after another with your default quality and format."))
        self._relayout()

    def cancel_download(self) -> None:
        if self.stage != "downloading" or self.focus_job is None:
            return
        self.queue.cancel(self.focus_job.id)
        self.cancel_btn.configure(state="disabled", text="Cancelling…")
        self.progress_text.configure(text="Cancelling and removing partial files…")
        log.info("Cancel requested")

    def _detach_focus_job(self) -> None:
        """The user moved on while a download runs: keep it going in the Queue panel."""
        if self.focus_job is not None and self.focus_job.status in ("waiting", "running"):
            log.info("Download #%d continues in the queue", self.focus_job.id)
        self.focus_job = None
        self._set_options_enabled(True)
        self.progress_bar.stop()
        self._refresh_queue(relayout=False)

    def _on_progress(self, p: Progress) -> None:
        if self.stage != "downloading":
            return
        if self.focus_job is not None and self.focus_job.engine is not None and self.focus_job.engine.cancelled:
            return
        text, percent = _progress_text(p)
        if percent is None:
            if self.progress_bar.cget("mode") != "indeterminate":
                self.progress_bar.configure(mode="indeterminate")
                self.progress_bar.start()
        else:
            if self.progress_bar.cget("mode") != "determinate":
                self.progress_bar.stop()
                self.progress_bar.configure(mode="determinate")
            self.progress_bar.set(max(0.0, min(1.0, percent / 100)))
        self.progress_text.configure(text=text)

    def _on_job_done(self, qj: QueuedJob) -> None:
        r = qj.result or DownloadResult()
        if not r.cancelled:
            self._record_history(qj)
            if self.settings.notify_when_done:
                notify_done(self)
        if qj is self.focus_job:
            self.focus_job = None
            self._on_download_done(r)
        self._refresh_queue()

    def _record_history(self, qj: QueuedJob) -> None:
        r = qj.result
        m = qj.meta
        title = qj.title if m.get("title") in (None, "", m.get("url")) else m["title"]
        err = r.failures[0][1].message if r.failures else ""
        entry = HistoryEntry(
            url=m.get("url", qj.job.urls[0]), title=title or m.get("url", ""), ok=bool(r.files),
            site=m.get("site", ""), quality_key=m.get("quality_key", "best"), format_key=m.get("format_key", "original"),
            files=[str(f) for f in r.files], folder=m.get("folder", str(qj.job.folder)), error=err,
            count=len(qj.job.urls), failed_count=len(r.failures), used_login=m.get("used_login", False),
            referer=m.get("referer", ""), project=m.get("project", ""), clip=m.get("clip", []),
            subtitles=m.get("subtitles", ""), subtitles_auto=m.get("subtitles_auto", False),
            playlist_all=m.get("playlist_all", False),
        )
        self.history.add(entry)
        self._refresh_history()

    def _on_download_done(self, r: DownloadResult) -> None:
        self.progress_bar.stop()
        self._set_options_enabled(True)
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
            text = f"Saved {len(r.files)} files to {elide_middle(str(r.files[0].parent), 60)}"
        self.done_text.configure(text=text)
        self._only("done")
        if r.failures:
            n = len(r.failures)
            first = r.failures[0][1]
            detail = "\n\n".join(f"{title}\n{info.message}\n{info.detail}" for title, info in r.failures)
            self.show_banner(ErrorInfo(Status.UNKNOWN,
                                       f"{n} video{'s' if n > 1 else ''} couldn't be downloaded. {first.message}",
                                       detail))
            self.banner.configure(fg_color=theme.WARN_BG)
            self.banner_dot.configure(text_color=theme.WARN)
        self._relayout()
        self.show_btn.focus_set()

    def show_saved(self) -> None:
        if self.saved_files:
            show_in_folder(self.saved_files[0])

    def download_another(self) -> None:
        self._detach_focus_job()
        self._token += 1
        self.stage = "empty"
        self.folder_override = None
        self._checked_url = None
        self._clear_result()
        self.password_var.set("")
        self.referer_var.set("")
        self.clip_var.set(False)
        self.clip_start.delete(0, "end")
        self.clip_end.delete(0, "end")
        self._on_clip_toggled(relayout=False)
        self.use_login_var.set(False)
        self.url_var.set("")
        self._only()
        self._refresh_folder_label()
        self._relayout()
        self.url_entry.focus_set()

    def _set_options_enabled(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        for w in (self.quality_menu, self.format_menu, self.subs_menu, self.playlist_choice, self.login_check):
            try:
                w.configure(state=state)
            except (tk.TclError, ValueError):
                pass

    # ======================================================================================
    # Queue panel
    # ======================================================================================

    def _refresh_queue(self, relayout: bool = True) -> None:
        jobs = [j for j in self.queue.active() if j is not self.focus_job]
        ids = {j.id for j in jobs}
        for jid in list(self._queue_rows):
            if jid not in ids:
                self._queue_rows.pop(jid)["frame"].destroy()
        for i, qj in enumerate(jobs):
            row = self._queue_rows.get(qj.id)
            if row is None:
                row = self._make_queue_row(qj)
                self._queue_rows[qj.id] = row
            row["frame"].grid(row=i, column=0, sticky="ew", padx=8, pady=(6 if i == 0 else 2, 6))
            self._update_queue_row(qj)
        self.queue_title.configure(text=f"Queue ({len(jobs)})" if jobs else "Queue")
        if relayout:
            self._relayout()

    def _make_queue_row(self, qj: QueuedJob) -> dict:
        f = ctk.CTkFrame(self.queue_list, fg_color="transparent")
        f.grid_columnconfigure(0, weight=1)
        title = ctk.CTkLabel(f, text="", anchor="w")
        title.grid(row=0, column=0, sticky="ew")
        status = ctk.CTkLabel(f, text="", anchor="w", text_color=MUTED)
        status.grid(row=1, column=0, sticky="ew")
        bar = ctk.CTkProgressBar(f, height=6)
        bar.set(0)
        x = ctk.CTkButton(f, text="✕", width=28, height=28, fg_color="transparent", hover_color=theme.SURFACE_2,
                          text_color=MUTED, command=lambda: self.queue.cancel(qj.id))
        x.grid(row=0, column=1, rowspan=2, padx=(6, 0))
        Tooltip(x, "Cancel this download")
        return {"frame": f, "title": title, "status": status, "bar": bar}

    def _update_queue_row(self, qj: QueuedJob) -> None:
        row = self._queue_rows.get(qj.id)
        if row is None:
            return
        row["title"].configure(text=elide_middle(qj.title, 64))
        if qj.status == "running" and qj.progress is not None:
            text, percent = _progress_text(qj.progress)
            row["status"].configure(text=text)
            if percent is not None:
                row["bar"].grid(row=2, column=0, sticky="ew", pady=(2, 0))
                row["bar"].set(percent / 100)
        elif qj.status == "running":
            row["status"].configure(text="Starting…")
        else:
            row["status"].configure(text="Waiting")
            row["bar"].grid_remove()

    # ======================================================================================
    # History panel
    # ======================================================================================

    def _refresh_history(self) -> None:
        for child in list(self.history_list.winfo_children()):
            child.destroy()
        entries = self.history.entries[:HISTORY_ROWS_SHOWN]
        for i, entry in enumerate(entries):
            self._make_history_row(i, entry)
        self.history_title.configure(text=f"History ({len(self.history.entries)})" if entries else "History")

    def _make_history_row(self, i: int, entry: HistoryEntry) -> None:
        color = theme.OK if entry.ok else theme.ERROR
        row = ctk.CTkFrame(self.history_list, fg_color=theme.OK_BG if entry.ok else theme.ERROR_BG, corner_radius=6)
        row.grid(row=i, column=0, sticky="ew", padx=4, pady=2)
        row.grid_columnconfigure(1, weight=1)
        bar = ctk.CTkFrame(row, width=4, height=30, fg_color=color, corner_radius=2)  # CTkFrame defaults to 200 px
        bar.grid(row=0, column=0, rowspan=2, sticky="ns", padx=(6, 8), pady=6)
        title = ctk.CTkLabel(row, text=elide_middle(entry.title, 70), anchor="w", cursor="hand2")
        title.grid(row=0, column=1, sticky="ew", pady=(4, 0))
        fmt = format_by_key(entry.format_key)
        bits = [entry.site, fmt.label if fmt.key != "original" else "", history_mod.relative_time(entry.when)]
        if entry.count > 1:
            bits.insert(0, f"{entry.count - entry.failed_count} of {entry.count} videos")
        if not entry.ok and entry.error:
            bits.append(entry.error)
        meta = ctk.CTkLabel(row, text="  ·  ".join(b for b in bits if b), anchor="w", text_color=MUTED,
                            cursor="hand2", justify="left")
        meta.grid(row=1, column=1, sticky="ew", pady=(0, 4))
        if entry.ok and entry.first_file():
            ctk.CTkButton(row, text="Show", width=54, height=26, command=lambda e=entry: self._show_history_file(e),
                          **self._secondary).grid(row=0, column=2, rowspan=2, padx=6)
        for w in (row, title, meta, bar):
            w.bind("<Button-1>", lambda _e, en=entry: self.redownload(en))
            w.bind("<Button-3>", lambda ev, en=entry: self._history_menu(ev, en))
        Tooltip(title, "Click to download again" if entry.ok else "Click to try again")

    def _show_history_file(self, entry: HistoryEntry) -> None:
        f = entry.first_file()
        if f:
            show_in_folder(f)

    def _history_menu(self, event, entry: HistoryEntry) -> None:
        menu = tk.Menu(self, tearoff=0, bg=theme.SURFACE, fg=theme.TEXT, activebackground=theme.SURFACE_2,
                       activeforeground=theme.TEXT)
        menu.add_command(label="Download again", command=lambda: self.redownload(entry))
        if entry.first_file():
            menu.add_command(label="Show in folder", command=lambda: self._show_history_file(entry))
        menu.add_command(label="Copy link", command=lambda: self._copy_text(entry.url))
        menu.add_separator()
        menu.add_command(label="Remove from history", command=lambda: self.remove_history(entry))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _copy_text(self, text: str) -> None:
        self.clipboard_clear()
        self.clipboard_append(text)
        self._last_clipboard = text  # don't treat our own copy as a newly copied link

    def redownload(self, entry: HistoryEntry) -> None:
        """Put a past download back in the form with the same options and check it."""
        log.info("Re-checking a download from history")
        self.download_another()
        self._redo = entry
        if entry.referer:
            self.referer_var.set(entry.referer)
            self._visible.add("referer")
        self.url_var.set(entry.url)
        self.use_login_var.set(entry.used_login and bool(self.login_candidates(normalize_url(entry.url))))
        self._checked_url = normalize_url(entry.url)  # keep start_check from resetting the login choice
        self.start_check()

    def _apply_redo(self, res: CheckResult) -> None:
        entry, self._redo = self._redo, None
        q = next((q for q in res.qualities if q.key == entry.quality_key), None)
        if q:
            self.quality_menu.set(q.display)
        fmt = format_by_key(entry.format_key)
        if fmt in res.formats:
            self.format_menu.set(fmt.label)
        if entry.subtitles:
            sub = next((s for s in res.subtitles if s.lang == entry.subtitles), None)
            if sub:
                self.subs_menu.set(sub.label)
        if entry.project:
            self.project_box.set(entry.project)
        if entry.clip:
            self.clip_var.set(True)
            self.clip_start.delete(0, "end")
            self.clip_start.insert(0, format_timecode(entry.clip[0]))
            self.clip_end.delete(0, "end")
            self.clip_end.insert(0, format_timecode(entry.clip[1]))
        if (entry.project or entry.clip or entry.subtitles) and not self._more_open:
            self.toggle_more()
        if entry.playlist_all and "playlist" in self._visible:
            values = self.playlist_choice.cget("values")
            if values:
                self.playlist_choice.set(values[-1])

    def remove_history(self, entry: HistoryEntry) -> None:
        self.history.remove(entry.id)
        self._refresh_history()
        self._relayout()

    def clear_history(self, confirm: bool = True) -> None:
        if not self.history.entries:
            return
        if confirm and not messagebox.askyesno(
                APP_TITLE, "Clear the download history? Your downloaded files stay where they are.", parent=self):
            return
        self.history.clear()
        self._refresh_history()
        self._relayout()

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
        from .ui_settings import SettingsWindow
        existing = getattr(self, "_settings_win", None)
        if existing is not None and existing.winfo_exists():
            existing.focus_set()
            return
        self._settings_win = SettingsWindow(self, self.settings, self.login_sources, on_saved=self._on_settings_saved,
                                            on_engine_updated=self.on_engine_updated,
                                            save_settings=self._save_settings)

    def _on_settings_saved(self, new: Settings) -> None:
        self.settings = new
        if not self.login_candidates():
            self.use_login_var.set(False)
        self._update_login_row()
        self._refresh_folder_label()
        if self.result and self.result.ok and self.stage == "checked":
            chosen = default_quality(self.result.qualities, new.default_quality)
            if chosen:
                self.quality_menu.set(chosen.display)
            fmt = format_by_key(new.default_format)
            if fmt in self.result.formats:
                self.format_menu.set(fmt.label)
                self._on_format_changed(relayout=False)
        self._relayout()

    # ======================================================================================
    # Events, keys, drag and drop, clipboard, window
    # ======================================================================================

    def _poll(self) -> None:
        last_progress: Progress | None = None
        job_progress: dict[int, QueuedJob] = {}
        try:
            while True:
                kind, token, payload = self.events.get_nowait()
                if token is not None and token != self._token:
                    continue  # result of a check the user has moved on from
                if kind == "job_progress":
                    qj, p = payload
                    if qj is self.focus_job:
                        last_progress = p  # only the newest progress matters
                    else:
                        job_progress[qj.id] = qj
                    continue
                if kind == "check":
                    self._on_check_done(payload)
                elif kind == "check_attempt":
                    self.spinner_label.configure(text=f"Trying your login from {payload.app_name}…"
                                                 if not payload.is_none else "Trying without a login…")
                elif kind == "thumb":
                    self._on_thumbnail(payload)
                elif kind in ("job_added", "job_started"):
                    self._refresh_queue()
                elif kind == "job_done":
                    if payload is self.focus_job:
                        last_progress = None
                    self._on_job_done(payload)
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
        try:
            if last_progress is not None:
                self._on_progress(last_progress)
            for qj in job_progress.values():
                self._update_queue_row(qj)
        except Exception:  # noqa: BLE001
            log.exception("Error while showing progress")
        self._poll_id = self.after(POLL_MS, self._poll)

    def _bind_keys(self) -> None:
        self.bind("<Return>", self._on_enter)
        self.bind("<KP_Enter>", self._on_enter)
        self.bind("<Escape>", self._on_escape)
        paste_keys = ("<Control-v>", "<Control-V>")
        if sys.platform == "darwin":
            paste_keys += ("<Command-v>", "<Command-V>")  # ⌘V on a Mac
        for seq in paste_keys:
            self.bind(seq, self._on_ctrl_v)
            self.url_entry.bind(seq, self._paste_into_url)
        self.url_entry.bind("<<Paste>>", self._paste_into_url)
        self.bind("<FocusIn>", self._on_focus_in, add="+")

    def _enable_drag_and_drop(self) -> None:
        if TkinterDnD is None:
            return
        try:
            self.TkdndVersion = TkinterDnD._require(self)
            self.drop_target_register(DND_TEXT)
            self.dnd_bind("<<Drop>>", self._on_drop)
        except Exception as e:  # noqa: BLE001 - drag and drop is a convenience
            log.info("Drag and drop unavailable: %s", e)

    def _on_drop(self, event):
        log.info("Link dropped onto the window")
        self.accept_text(event.data or "")
        return getattr(event, "action", None)

    def _on_focus_in(self, event=None) -> None:
        """When the app comes to the front with an empty form, use a video link just copied."""
        if event is not None and event.widget is not self:
            return
        if not self.settings.use_copied_links or self.stage != "empty" or self.url_var.get().strip():
            return
        try:
            text = self.clipboard_get().strip()
        except tk.TclError:
            return
        if not text or text == self._last_clipboard or len(text) > 4096:
            return
        self._last_clipboard = text
        urls = extract_urls(text)
        if len(urls) == 1 and text.startswith(("http://", "https://", "www.")) and " " not in text:
            log.info("Using a link from the clipboard")
            self.url_var.set(urls[0])
            self.start_check()

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
            return None  # normal paste into the password/page/clip/project fields
        return self._paste_into_url(event)

    def _paste_into_url(self, _event=None):
        self.paste_url()
        return "break"

    def _restore_geometry(self) -> None:
        geo = self.settings.window_geometry
        default = "640x260"
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
        if self.queue.busy:
            n = len(self.queue.active())
            what = "A download is" if n == 1 else f"{n} downloads are"
            if not messagebox.askyesno(APP_TITLE, f"{what} in progress. Cancel and quit?", parent=self):
                return
            self.queue.cancel_all()
            self.queue.join(timeout=15)  # let the engine delete partial files
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


def _progress_text(p: Progress) -> tuple[str, float | None]:
    """Plain-English progress line and the percent for the bar (None = unknown)."""
    prefix = f"Video {p.item} of {p.items} — " if p.items > 1 else ""
    if p.phase == "starting":
        return prefix + "Starting…", None
    if p.phase == "processing":
        return prefix + "Finishing up…", None
    if p.phase == "converting":
        if p.percent is None:
            return prefix + "Converting…", None
        return prefix + f"Converting for editing… {p.percent:.0f}%", p.percent
    bits = []
    if p.percent is not None:
        bits.append(f"{p.percent:.0f}%")
    elif p.downloaded:
        bits.append(f"{human_size(p.downloaded)} so far")
    if p.speed:
        bits.append(f"{human_size(p.speed)}/s")
    if p.eta is not None and p.percent is not None:
        bits.append(_eta_text(p.eta))
    return prefix + ("  ·  ".join(bits) or "Downloading…"), p.percent


def _eta_text(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return "less than a minute left" if s > 5 else "almost done"
    m = round(s / 60)
    if m < 60:
        return f"about {m} min left"
    h, m = divmod(m, 60)
    return f"about {h} h {m} min left"
