"""Drive the real windows headlessly (needs a display; CI uses Windows, Linux uses xvfb-run).

A fake engine that *blocks* like the real one checks every banner state and interaction;
a final test uses the real engine against the local media server and measures that the
UI thread never stalls.
"""

import threading
import time
import tkinter as tk
from pathlib import Path

import pytest

from app.browsers import NONE_SOURCE, LoginSource
from app.engine import (
    CheckResult,
    DownloadResult,
    PlaylistInfo,
    Progress,
    Quality,
    VideoInfo,
    normalize_url,
)
from app.errors import Status, classify_error, ready
from app.settings import Settings


def _display_available() -> bool:
    try:
        root = tk.Tk()
        root.destroy()
        return True
    except tk.TclError:
        return False


pytestmark = [pytest.mark.gui, pytest.mark.skipif(not _display_available(), reason="no display")]

ZEN = LoginSource("firefox:/zen", "Zen — Default (release)", "Zen", "firefox", "/zen")
CHROME = LoginSource("chrome", "Chrome (may not work)", "Chrome", "chrome", None,
                     note="Windows encryption usually blocks reading logins from Chrome, Edge and Brave.")
QUALITIES = [
    Quality("best", "Best available (MP4) — 1080p", size=50_000_000),
    Quality("h1080", "1080p (Full HD)", 1080, size=50_000_000),
    Quality("h720", "720p (HD)", 720, size=20_000_000),
    Quality("audio", "Audio only (MP3)", audio_only=True, size=3_000_000),
]


class FakeEngine:
    """Blocks like the real engine (sleep in the worker thread) and answers by URL keyword."""

    def __init__(self, delay=0.3, step=0.02):
        self.delay, self.step = delay, step
        self._cancel = threading.Event()
        self.requests, self.jobs = [], []

    @property
    def cancelled(self):
        return self._cancel.is_set()

    def cancel(self):
        self._cancel.set()

    def check_link(self, req):
        self.requests.append(req)
        time.sleep(self.delay)
        url = normalize_url(req.url)
        if not url:
            return CheckResult(classify_error(f"{req.url!r} is not a valid URL"))
        if "login" in url and req.login.is_none:
            return CheckResult(classify_error("This video is only available for registered users"))
        if "password" in url and req.password != "secret":
            msg = "Wrong video password" if req.password else "protected by a password, use the --video-password option"
            return CheckResult(classify_error(msg, password_given=bool(req.password)))
        if "embed" in url and not req.referer:
            return CheckResult(classify_error("Cannot download embed-only video without embedding URL"))
        if "drm" in url:
            return CheckResult(classify_error("This video is DRM protected"))
        playlist = None
        if "list=" in url:
            playlist = PlaylistInfo("Mix: best of", 3, [f"{url}&i={i}" for i in range(3)], pure=False)
        video = VideoInfo(f"Title for {url}", "Uploader", 125, None, url, "Youtube")
        return CheckResult(ready(), url=url, video=video, qualities=list(QUALITIES), playlist=playlist)

    def download(self, job, on_progress):
        self.jobs.append(job)
        self._cancel.clear()
        files = []
        for i, _url in enumerate(job.urls, 1):
            for k in range(20):
                if self._cancel.is_set():
                    return DownloadResult(files=files, cancelled=True)
                time.sleep(self.step)
                on_progress(Progress(k * 5, 2_000_000, 30, k, 20, "downloading", i, len(job.urls), "T"))
            job.folder.mkdir(parents=True, exist_ok=True)
            f = job.folder / f"video{i}.{job.quality.ext}"
            f.write_bytes(b"x")
            files.append(f)
        return DownloadResult(files=files)


def pump(app, seconds=0.2, until=None, stalls=None):
    """Run the Tk loop. Records how long each update() took (a long one = a frozen UI)."""
    end = time.time() + seconds
    while time.time() < end:
        t0 = time.perf_counter()
        app.update()
        if stalls is not None:
            stalls.append(time.perf_counter() - t0)
        if until and until():
            return True
        time.sleep(0.005)
    return bool(until and until())


def press(app, sequence):
    """Send a key to the URL field, the way a user types it."""
    app.url_entry._entry.focus_force()
    app.update()
    app.url_entry._entry.event_generate(sequence)


def visible(app, name):
    return app.sections[name].winfo_ismapped()


@pytest.fixture
def make_app(tmp_path):
    import customtkinter as ctk
    ctk.set_appearance_mode("dark")
    from app.ui_main import MainWindow

    apps = []

    def _make(engine=None, settings=None, sources=None):
        saved = []
        app = MainWindow(settings or Settings(save_folder=str(tmp_path / "out")),
                         engine or FakeEngine(), login_sources=sources or [NONE_SOURCE, ZEN, CHROME],
                         check_updates=False, save_settings=saved.append)
        (tmp_path / "out").mkdir(exist_ok=True)
        app.saved_settings = saved
        apps.append(app)
        pump(app, 0.3)
        return app

    yield _make
    for a in apps:
        try:
            a.destroy()
        except tk.TclError:
            pass


def check(app, url, timeout=5):
    app.url_var.set(url)
    app.start_check()
    assert pump(app, timeout, lambda: app.stage == "checked"), "check did not finish"


# ---- first screen + auto check -------------------------------------------------------------------

def test_first_screen_shows_only_url_field(make_app):
    app = make_app()
    shown = [n for n in app.sections if visible(app, n)]
    assert shown == ["header", "url"]


def test_typing_a_url_auto_checks_after_debounce(make_app):
    eng = FakeEngine()
    app = make_app(eng)
    app.url_var.set("https://www.youtube.com/watch?v=abc")
    pump(app, 0.4)
    assert eng.requests == []  # still inside the 600 ms debounce
    assert pump(app, 3, lambda: app.stage == "checked")
    assert len(eng.requests) == 1
    assert app.banner_status is Status.READY


def test_garbage_does_not_auto_check_but_manual_check_says_invalid(make_app):
    eng = FakeEngine()
    app = make_app(eng)
    app.url_var.set("asdf qwerty")
    pump(app, 0.9)
    assert eng.requests == []
    app.start_check()
    assert pump(app, 3, lambda: app.stage == "checked")
    assert app.banner_status is Status.INVALID_URL
    assert not visible(app, "download")


def test_ready_shows_preview_quality_and_default_from_settings(make_app, tmp_path):
    app = make_app(settings=Settings(save_folder=str(tmp_path / "out"), default_quality="720"))
    check(app, "https://vimeo.com/123")
    pump(app, 0.2)
    for name in ("banner", "preview", "options", "download"):
        assert visible(app, name), name
    assert app.quality_menu.get().startswith("720p (HD)")
    assert "~" in app.quality_menu.get()  # approximate size shown
    assert app.title_label.cget("text").startswith("Title for")
    assert "2:05" in app.meta_label.cget("text")
    assert not visible(app, "login")
    assert not visible(app, "playlist")


def test_stale_check_result_is_ignored(make_app):
    eng = FakeEngine(delay=0.6)
    app = make_app(eng)
    app.url_var.set("https://example.com/drm")
    app.start_check()
    pump(app, 0.1)
    app.url_var.set("https://example.com/fine")
    app.start_check()
    assert pump(app, 4, lambda: app.stage == "checked")
    pump(app, 0.8)
    assert app.banner_status is Status.READY


# ---- yellow / red states --------------------------------------------------------------------------

def test_needs_login_reveals_dropdown_and_retry_then_succeeds(make_app):
    eng = FakeEngine()
    app = make_app(eng)
    check(app, "https://vimeo.com/login-private")
    pump(app, 0.2)
    assert app.banner_status is Status.NEEDS_LOGIN
    assert "Use login from" in app.banner_text.cget("text")
    assert visible(app, "login") and app.login_retry.winfo_ismapped()
    app.login_menu.set(ZEN.label)
    app._on_login_selected(ZEN.label)
    app.login_retry.invoke()
    assert pump(app, 3, lambda: app.stage == "checked")
    assert app.banner_status is Status.READY
    assert eng.requests[-1].login == ZEN
    pump(app, 0.2)
    assert visible(app, "login")  # stays visible so you can see which login is used


def test_chrome_login_shows_windows_encryption_note(make_app):
    app = make_app()
    check(app, "https://vimeo.com/login-private")
    app._on_login_selected(CHROME.label)
    pump(app, 0.2)
    assert app.login_note.winfo_ismapped()
    assert "Windows encryption" in app.login_note.cget("text")


def test_needs_password_reveals_field_and_never_keeps_wrong_password_state(make_app):
    eng = FakeEngine()
    app = make_app(eng)
    check(app, "https://vimeo.com/password-video")
    pump(app, 0.2)
    assert app.banner_status is Status.NEEDS_PASSWORD
    assert visible(app, "password")
    app.password_var.set("nope")
    app.retry()
    assert pump(app, 3, lambda: app.stage == "checked")
    assert "didn't work" in app.banner_text.cget("text")
    app.password_var.set("secret")
    app.retry()
    assert pump(app, 3, lambda: app.stage == "checked")
    assert app.banner_status is Status.READY
    assert eng.requests[-1].password == "secret"


def test_embed_restricted_reveals_page_field_passed_as_referer(make_app):
    eng = FakeEngine()
    app = make_app(eng)
    check(app, "https://player.vimeo.com/video/1/embed")
    pump(app, 0.2)
    assert app.banner_status is Status.EMBED_RESTRICTED
    assert "only plays on a specific website" in app.banner_text.cget("text")
    assert visible(app, "referer")
    app.referer_var.set("https://school.example/lesson")
    app.retry()
    assert pump(app, 3, lambda: app.stage == "checked")
    assert eng.requests[-1].referer == "https://school.example/lesson"
    assert app.banner_status is Status.READY


def test_drm_is_red_with_no_workaround(make_app):
    app = make_app()
    check(app, "https://example.com/drm")
    pump(app, 0.2)
    assert app.banner_status is Status.DRM
    assert app.banner_text.cget("text") == "This video is copy-protected and can't be downloaded."
    for name in ("login", "password", "referer", "download", "options"):
        assert not visible(app, name), name


def test_details_box_only_in_details(make_app):
    app = make_app()
    check(app, "https://example.com/drm")
    pump(app, 0.2)
    assert "DRM protected" not in app.banner_text.cget("text")
    app.toggle_details()
    pump(app, 0.1)
    assert app.details_box.winfo_ismapped()
    assert "DRM protected" in app.details_box.get("1.0", "end")


# ---- download ------------------------------------------------------------------------------------

def test_enter_downloads_with_progress_and_done_state(make_app, tmp_path, monkeypatch):
    shown = []
    monkeypatch.setattr("app.ui_main.show_in_folder", shown.append)
    eng = FakeEngine()
    app = make_app(eng)
    check(app, "https://vimeo.com/1")
    texts = set()
    press(app, "<Return>")  # Enter downloads when ready
    pump(app, 0.05)
    assert app.stage == "downloading"
    assert pump(app, 5, lambda: (texts.add(app.progress_text.cget("text")), app.stage == "done")[1])
    assert any("%" in t and "/s" in t for t in texts), texts
    assert app.done_text.cget("text") == "Saved: video1.mp4"
    assert eng.jobs[0].folder == tmp_path / "out"
    app.show_btn.invoke()
    assert shown == [tmp_path / "out" / "video1.mp4"]
    app.download_another()
    pump(app, 0.2)
    assert app.url_var.get() == ""
    assert [n for n in app.sections if visible(app, n)] == ["header", "url"]


def test_escape_cancels_download(make_app):
    eng = FakeEngine(step=0.05)
    app = make_app(eng)
    check(app, "https://vimeo.com/1")
    app.start_download()
    pump(app, 0.2)
    press(app, "<Escape>")
    assert pump(app, 3, lambda: app.stage == "checked")
    assert app.banner_status is Status.CANCELLED
    assert visible(app, "download")


def test_audio_quality_and_one_off_folder(make_app, tmp_path):
    eng = FakeEngine()
    app = make_app(eng)
    check(app, "https://vimeo.com/1")
    other = tmp_path / "other"
    other.mkdir()
    app.folder_override = other
    app.quality_menu.set(next(q.display for q in QUALITIES if q.audio_only))
    app.start_download()
    assert pump(app, 5, lambda: app.stage == "done")
    assert eng.jobs[0].quality.audio_only
    assert eng.jobs[0].folder == other
    assert app.done_text.cget("text") == "Saved: video1.mp3"
    app.download_another()
    assert app.folder_override is None  # one-off


def test_playlist_question_defaults_to_just_this_video(make_app, tmp_path):
    eng = FakeEngine()
    app = make_app(eng)
    check(app, "https://www.youtube.com/watch?v=a&list=PL1")
    pump(app, 0.2)
    assert visible(app, "playlist")
    assert "all 3 videos" in app.playlist_label.cget("text")
    assert app.playlist_choice.get() == "Just this video"
    app.start_download()
    assert pump(app, 5, lambda: app.stage == "done")
    assert len(eng.jobs[-1].urls) == 1

    check(app, "https://www.youtube.com/watch?v=a&list=PL2")
    app.playlist_choice.set("All 3 videos")
    app.start_download()
    assert pump(app, 8, lambda: app.stage == "done")
    job = eng.jobs[-1]
    assert len(job.urls) == 3
    assert job.folder.parent == tmp_path / "out"
    assert app.done_text.cget("text").startswith("Saved 3 videos")


def test_bad_save_folder_is_reported_not_crashing(make_app, tmp_path):
    app = make_app(settings=Settings(save_folder=str(tmp_path / "missing")))
    check(app, "https://vimeo.com/1")
    app.start_download()
    pump(app, 0.2)
    assert app.stage == "checked"
    assert app.banner_status is Status.SAVE_FAILED


# ---- keyboard ------------------------------------------------------------------------------------

def test_ctrl_v_pastes_into_url_and_checks(make_app):
    eng = FakeEngine()
    app = make_app(eng)
    app.clipboard_clear()
    app.clipboard_append("https://vimeo.com/42")
    app.url_entry._entry.focus_force()
    pump(app, 0.1)
    app.url_entry._entry.event_generate("<Control-v>")
    assert pump(app, 3, lambda: app.stage == "checked")
    assert app.url_var.get() == "https://vimeo.com/42"
    assert len(eng.requests) == 1


def test_enter_checks_when_nothing_checked(make_app):
    eng = FakeEngine()
    app = make_app(eng)
    app.url_var.set("https://vimeo.com/7")
    press(app, "<Return>")
    assert pump(app, 3, lambda: app.stage == "checked")
    assert len(eng.requests) == 1


# ---- settings + persistence ----------------------------------------------------------------------

def test_settings_window_saves_and_updates_main(make_app, tmp_path):
    from app.settings import QUALITY_LABELS
    app = make_app()
    app.open_settings()
    pump(app, 0.3)
    win = app._settings_win
    new_folder = tmp_path / "videos"
    new_folder.mkdir()
    win._folder = str(new_folder)
    win.quality_menu.set(QUALITY_LABELS["1080"])
    win.login_menu.set(ZEN.label)
    win.auto_update_var.set(False)
    win.save()
    pump(app, 0.2)
    [saved] = app.saved_settings
    assert saved.save_folder == str(new_folder)
    assert saved.default_quality == "1080"
    assert saved.login_source == ZEN.key
    assert saved.check_updates_on_launch is False
    assert app.settings == saved
    assert app.login == ZEN
    assert str(new_folder) in app.folder_label.cget("text") or "…" in app.folder_label.cget("text")


def test_settings_rejects_missing_folder(make_app, tmp_path):
    app = make_app()
    app.open_settings()
    pump(app, 0.3)
    win = app._settings_win
    win._folder = str(tmp_path / "nope")
    win.save()
    pump(app, 0.1)
    assert win.winfo_exists()
    assert "doesn't exist" in win.error_label.cget("text")
    assert app.saved_settings == []
    win.destroy()


def test_folder_setting_persists_across_restart(tmp_path, monkeypatch):
    """Acceptance 8, automated: save through the Settings window, 'restart', and read it back."""
    import customtkinter as ctk
    from app import settings as settings_mod
    from app.ui_main import MainWindow
    monkeypatch.setenv("VD_APPDATA", str(tmp_path / "appdata"))
    folder = tmp_path / "Videos"
    folder.mkdir()
    ctk.set_appearance_mode("dark")
    app = MainWindow(settings_mod.load(), FakeEngine(), login_sources=[NONE_SOURCE], check_updates=False)
    pump(app, 0.3)
    app.open_settings()
    pump(app, 0.3)
    app._settings_win._folder = str(folder)
    app._settings_win.save()
    pump(app, 0.1)
    app._on_close()
    restarted = settings_mod.load()
    assert restarted.effective_save_folder() == folder
    assert restarted.window_geometry  # size/position remembered too
    app2 = MainWindow(restarted, FakeEngine(), login_sources=[NONE_SOURCE], check_updates=False)
    pump(app2, 0.3)
    assert str(folder) in app2.folder_label.cget("text") or "…" in app2.folder_label.cget("text")
    app2.destroy()


def test_minimum_width(make_app):
    app = make_app()
    app.geometry("300x200")
    pump(app, 0.3)
    assert app.winfo_width() >= 500


# ---- real engine: the UI never freezes -----------------------------------------------------------

def test_ui_stays_responsive_with_real_engine(make_app, media_server, tmp_path):
    from app.engine import Engine
    app = make_app(Engine())
    stalls = []
    app.url_var.set(f"{media_server}/dashslow/manifest.mpd")
    app.start_check()
    assert pump(app, 30, lambda: app.stage == "checked", stalls)
    assert app.banner_status is Status.READY, app.banner_text.cget("text")
    app.start_download()
    progressed = pump(app, 30, lambda: app.progress_bar.cget("mode") == "determinate"
                      and app.progress_bar.get() > 0.05, stalls)
    assert progressed
    app.cancel_download()
    assert pump(app, 30, lambda: app.stage == "checked", stalls)
    assert app.banner_status is Status.CANCELLED
    assert list((tmp_path / "out").iterdir()) == []  # partial files removed
    worst = max(stalls)
    assert worst < 0.25, f"UI thread blocked for {worst * 1000:.0f} ms"
