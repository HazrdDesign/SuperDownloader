"""Drive the real windows headlessly (needs a display; CI uses Windows, Linux uses xvfb-run).

A fake engine that *blocks* like the real one checks every banner state and interaction;
the last tests use the real engine against the local media server and measure that the
UI thread never stalls.
"""

import threading
import time
import tkinter as tk
from pathlib import Path

import pytest

from app.browsers import NONE_SOURCE, LoginSource
from app.engine import (
    FORMATS_BY_KEY,
    CheckResult,
    Engine,
    DownloadResult,
    PlaylistInfo,
    Progress,
    Quality,
    SubtitleChoice,
    VideoInfo,
    normalize_url,
)
from app.errors import Status, classify_error, ready
from app.history import History, HistoryEntry
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
    Quality("best", "Best (1080p)", size=50_000_000),
    Quality("h1080", "1080p (Full HD)", 1080, size=50_000_000),
    Quality("h720", "720p (HD)", 720, size=20_000_000),
]
SUBS = [SubtitleChoice("en", "English"), SubtitleChoice("en-orig", "English (auto-generated)", auto=True)]


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

    def check_link(self, req, on_attempt=None):
        self.requests.append(req)
        time.sleep(self.delay)
        return Engine.check_link(self, req, on_attempt)  # the real try-each-browser logic

    def _check_once(self, req):
        return self._check(req)

    def _check(self, req):
        url = normalize_url(req.url)
        if not url:
            return CheckResult(classify_error(f"{req.url!r} is not a valid URL"))
        if req.login.ydl_browser == "chrome":  # like Chrome on Windows: its login can't be read
            return CheckResult(classify_error("Failed to decrypt with DPAPI", browser=req.login.app_name))
        if "login" in url and (req.login.is_none or req.login.profile == "/signed-out"):
            return CheckResult(classify_error("This video is only available for registered users",
                                              browser=req.login.app_name))
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
        subs = list(SUBS) if "subs" in url else []
        video = VideoInfo(f"Title for {url}", "Uploader", 125, None, url, "Youtube")
        return CheckResult(ready(), url=url, video=video, qualities=list(QUALITIES), playlist=playlist,
                           subtitles=subs)

    def download(self, job, on_progress):
        self.jobs.append(job)
        self._cancel.clear()
        files, failures = [], []
        for i, url in enumerate(job.urls, 1):
            if "fail" in url:
                failures.append(("T", classify_error("Video unavailable. This video has been removed")))
                continue
            for k in range(20):
                if self._cancel.is_set():
                    return DownloadResult(files=files, cancelled=True)
                time.sleep(self.step)
                on_progress(Progress(k * 5, 2_000_000, 30, k, 20, "downloading", i, len(job.urls), f"Video {i}"))
            job.folder.mkdir(parents=True, exist_ok=True)
            f = job.folder / f"{job.name_prefix}video{i}-{len(self.jobs)}.{job.fmt.ext}"
            f.write_bytes(b"x")
            files.append(f)
        return DownloadResult(files=files, failures=failures)


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
    done = bool(until and until())
    if until and not done:
        # Show where every thread is stuck, to make a timeout on CI diagnosable.
        import faulthandler
        import sys
        print(f"pump timed out after {seconds}s; stage={getattr(app, 'stage', '?')}", file=sys.stderr)
        faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
    return done


def press(app, sequence):
    """Send a key to the URL field, the way a user types it."""
    app.url_entry._entry.focus_force()
    app.update()
    app.url_entry._entry.event_generate(sequence)


def visible(app, name):
    if name == "download":  # the Download button sits in the options row
        return bool(app.download_btn.winfo_ismapped())
    return app.sections[name].winfo_ismapped()


@pytest.fixture
def make_app(tmp_path):
    from app import theme
    from app.ui_main import MainWindow
    theme.apply()

    apps = []

    def _make(engine=None, settings=None, sources=None, history=None):
        saved = []
        (tmp_path / "out").mkdir(exist_ok=True)
        app = MainWindow(settings or Settings(save_folder=str(tmp_path / "out")),
                         engine or FakeEngine(), login_sources=sources or [NONE_SOURCE, ZEN, CHROME],
                         check_updates=False, save_settings=saved.append, warm_up_engine=False,
                         history=history if history is not None else History(persist=False))
        app.saved_settings = saved
        apps.append(app)
        pump(app, 0.3)
        return app

    yield _make
    for a in apps:
        try:
            a.queue.cancel_all()
            a.queue.join(5)
            a.destroy()
        except tk.TclError:
            pass
    # Collect destroyed windows' fonts/images here on the main thread; if a worker thread in a
    # later test collected them, Tk would make it wait for a main loop that pump() doesn't run.
    import gc
    gc.collect()


def check(app, url, timeout=5):
    app.url_var.set(url)
    app.start_check()
    assert pump(app, timeout, lambda: app.stage == "checked"), "check did not finish"


def finish(app, timeout=8):
    assert pump(app, timeout, lambda: not app.queue.busy and app.stage != "downloading"), "downloads did not finish"
    pump(app, 0.2)


# ---- first screen + auto check -------------------------------------------------------------------

def test_first_screen_shows_only_url_field(make_app):
    app = make_app()
    shown = [n for n in app.sections if visible(app, n)]
    assert shown == ["header", "url"]
    assert app.title() == "Super Downloader"


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


def test_only_the_link_field_no_paste_or_check_buttons(make_app):
    app = make_app()
    assert not hasattr(app, "paste_btn") and not hasattr(app, "check_btn")
    assert app.url_entry.grid_info()["columnspan"] in (1, "1")
    assert len(app.sections["url"].grid_slaves()) == 1


def test_network_problem_offers_try_again(make_app):
    class Offline(FakeEngine):
        def _check(self, req):
            from app.engine import CheckResult
            return CheckResult(classify_error("<urlopen error [Errno 11001] getaddrinfo failed>"))

    eng = Offline()
    app = make_app(eng)
    check(app, "https://www.youtube.com/watch?v=1")
    pump(app, 0.2)
    assert app.banner_status is Status.NETWORK
    assert app.banner_retry.winfo_ismapped()
    n = len(eng.requests)
    app.banner_retry._label.event_generate("<Button-1>")  # where a real click lands
    assert pump(app, 3, lambda: len(eng.requests) > n)


def test_ready_shows_preview_quality_format_and_defaults(make_app, tmp_path):
    app = make_app(settings=Settings(save_folder=str(tmp_path / "out"), default_quality="720",
                                     default_format="edit"))
    check(app, "https://www.youtube.com/watch?v=1")
    pump(app, 0.2)
    for name in ("preview", "options", "download"):
        assert visible(app, name), name
    assert app.banner_status is Status.READY and not visible(app, "banner")  # no green "Ready" banner
    # Quality, Format and Download on one row.
    rows = {w.grid_info()["row"] for w in (app.quality_menu, app.format_menu, app.download_btn)}
    assert len(rows) == 1
    assert app.quality_menu.get().startswith("720p (HD)")
    assert "~" in app.quality_menu.get()  # approximate size shown
    assert app.format_menu.get() == FORMATS_BY_KEY["edit"].label
    assert app.title_label.cget("text").startswith("Title for")
    assert "2:05" in app.meta_label.cget("text")
    assert not visible(app, "login") and not visible(app, "playlist") and not visible(app, "more")


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


# ---- login: one checkbox, automatic browser, automatic retry -------------------------------------

def test_login_is_retried_automatically_with_the_browser(make_app):
    """Needs-login sites just work: the app retries with each browser by itself."""
    eng = FakeEngine()
    app = make_app(eng)
    assert app.login_candidates()[0] == ZEN
    check(app, "https://example.com/login-private")
    pump(app, 0.2)
    assert app.banner_status is Status.READY
    assert eng.requests[-1].login == NONE_SOURCE and eng.requests[-1].fallback_logins == (ZEN, CHROME)
    assert app.result.login == ZEN
    assert visible(app, "login") and app.use_login_var.get()
    assert app.login_check.cget("text") == "Log in with my browser (for private or password-protected videos)"
    assert app.login_note.cget("text") == "Using your login from Zen."


def test_each_browser_is_tried_until_one_works_and_remembered(make_app, tmp_path):
    """The most recently used browser isn't logged in; the next one is. Next time it goes first."""
    now = time.time()
    firefox = LoginSource("firefox:/signed-out", "Firefox — default", "Firefox", "firefox", "/signed-out", now)
    zen = LoginSource("firefox:/zen", "Zen — Default (release)", "Zen", "firefox", "/zen", now - 3600)
    eng = FakeEngine()
    app = make_app(eng, sources=[NONE_SOURCE, firefox, zen])
    assert app.login_candidates() == [firefox, zen]
    check(app, "https://www.example.com/login-private")
    pump(app, 0.2)
    assert app.banner_status is Status.READY and app.result.login == zen
    assert app.spinner_label.cget("text") == "Trying your login from Zen…"
    assert app.saved_settings[-1].site_logins == {"example.com": zen.key}
    assert app.login_candidates("https://player.example.com/x") == [zen, firefox]
    # The download uses the login that worked.
    app.start_download()
    finish(app, 10)
    assert eng.jobs[-1].login == zen


def test_none_of_the_browsers_worked(make_app):
    now = time.time()
    a = LoginSource("firefox:/signed-out", "Firefox — default", "Firefox", "firefox", "/signed-out", now)
    eng = FakeEngine()
    app = make_app(eng, sources=[NONE_SOURCE, a, CHROME])
    check(app, "https://example.com/login-private")
    pump(app, 0.2)
    assert app.banner_status is Status.NEEDS_LOGIN
    text = app.banner_text.cget("text")
    assert "Firefox didn't work" in text and "Chrome" in text
    assert app.login_retry.winfo_ismapped()
    assert CHROME.key in app._unreadable_logins  # tried last from now on


def test_vimeo_link_shows_the_single_login_option_checked(make_app):
    eng = FakeEngine()
    app = make_app(eng)
    check(app, "https://vimeo.com/123")
    pump(app, 0.2)
    assert visible(app, "login")
    assert app.use_login_var.get()
    assert eng.requests[-1].login == ZEN
    assert eng.requests[-1].fallback_logins == (CHROME, NONE_SOURCE)  # then without a login
    assert app.login_check.cget("text") == "Log in with my browser (for private or password-protected videos)"


def test_single_browser_is_named(make_app):
    app = make_app(sources=[NONE_SOURCE, ZEN])
    check(app, "https://vimeo.com/123")
    pump(app, 0.2)
    assert "Log in with Zen (for private or password-protected videos)" == app.login_check.cget("text")


def test_never_use_login_setting(make_app, tmp_path):
    eng = FakeEngine()
    app = make_app(eng, settings=Settings(save_folder=str(tmp_path / "out"), login_source="none"))
    assert app.login_candidates() == []
    check(app, "https://example.com/login-private")
    pump(app, 0.2)
    assert app.banner_status is Status.NEEDS_LOGIN
    assert eng.requests[-1].fallback_logins == ()
    assert visible(app, "login")
    assert "Browser logins are turned off" in app.login_note.cget("text")


def test_no_browser_found(make_app):
    app = make_app(sources=[NONE_SOURCE, NONE_SOURCE])
    check(app, "https://example.com/login-private")
    pump(app, 0.2)
    assert app.banner_status is Status.NEEDS_LOGIN
    assert "log into the site in your browser" in app.login_note.cget("text")


def test_one_browser_chosen_in_settings_is_the_only_one_tried(make_app, tmp_path):
    eng = FakeEngine()
    app = make_app(eng, settings=Settings(save_folder=str(tmp_path / "out"), login_source=ZEN.key))
    check(app, "https://example.com/login-private")
    pump(app, 0.2)
    assert app.banner_status is Status.READY
    assert eng.requests[-1].fallback_logins == (ZEN,)
    assert app.saved_settings == []  # nothing to remember: there's only one browser


def test_chrome_in_settings_shows_encryption_note(make_app, tmp_path):
    app = make_app(settings=Settings(save_folder=str(tmp_path / "out"), login_source="chrome"))
    check(app, "https://vimeo.com/5")
    pump(app, 0.2)
    assert app.login_note.winfo_ismapped()
    assert "Windows encryption" in app.login_note.cget("text")


# ---- other yellow / red states ---------------------------------------------------------------------

def test_needs_password_reveals_field(make_app):
    eng = FakeEngine()
    app = make_app(eng)
    check(app, "https://example.com/password-video")
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
    check(app, "https://example.com/video/1/embed")
    pump(app, 0.2)
    assert app.banner_status is Status.EMBED_RESTRICTED
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
    for name in ("password", "referer", "download", "options"):
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


# ---- download --------------------------------------------------------------------------------------

def test_enter_downloads_with_progress_and_done_state(make_app, tmp_path, monkeypatch):
    shown = []
    monkeypatch.setattr("app.ui_main.show_in_folder", shown.append)
    eng = FakeEngine()
    app = make_app(eng)
    check(app, "https://www.youtube.com/watch?v=1")
    texts = set()
    press(app, "<Return>")  # Enter downloads when ready
    pump(app, 0.05)
    assert app.stage == "downloading"
    assert pump(app, 5, lambda: (texts.add(app.progress_text.cget("text")), app.stage == "done")[1])
    assert any("%" in t and "/s" in t for t in texts), texts
    assert app.done_text.cget("text") == "Saved: video1-1.mp4"
    assert eng.jobs[0].folder == tmp_path / "out"
    assert eng.jobs[0].fmt.key == "original"
    app.show_btn.invoke()
    assert shown == [tmp_path / "out" / "video1-1.mp4"]
    app.download_another()
    pump(app, 0.2)
    assert app.url_var.get() == ""
    assert [n for n in app.sections if visible(app, n)] == ["header", "url", "history"]


def test_escape_cancels_download_and_cancelled_is_not_in_history(make_app):
    eng = FakeEngine(step=0.05)
    app = make_app(eng)
    check(app, "https://www.youtube.com/watch?v=1")
    app.start_download()
    pump(app, 0.2)
    press(app, "<Escape>")
    assert pump(app, 3, lambda: app.stage == "checked")
    assert app.banner_status is Status.CANCELLED
    assert visible(app, "download")
    assert app.history.entries == []


def test_audio_format_hides_quality_and_one_off_folder(make_app, tmp_path):
    eng = FakeEngine()
    app = make_app(eng)
    check(app, "https://www.youtube.com/watch?v=1")
    other = tmp_path / "other"
    other.mkdir()
    app.folder_override = other
    app.format_menu.set(FORMATS_BY_KEY["mp3"].label)
    app._on_format_changed()
    pump(app, 0.1)
    assert not app.quality_menu.winfo_ismapped()
    app.start_download()
    finish(app)
    job = eng.jobs[0]
    assert job.fmt.key == "mp3" and job.quality is None
    assert job.folder == other
    assert app.done_text.cget("text") == "Saved: video1-1.mp3"
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
    finish(app)
    assert len(eng.jobs[-1].urls) == 1

    check(app, "https://www.youtube.com/watch?v=a&list=PL2")
    app.playlist_choice.set("All 3 videos")
    app.start_download()
    finish(app, 10)
    job = eng.jobs[-1]
    assert len(job.urls) == 3
    assert job.folder.parent == tmp_path / "out"
    assert app.done_text.cget("text").startswith("Saved 3 files")


def test_bad_save_folder_is_reported_not_crashing(make_app, tmp_path):
    blocker = tmp_path / "a-file"
    blocker.write_text("x")
    app = make_app(settings=Settings(save_folder=str(blocker)))
    check(app, "https://www.youtube.com/watch?v=1")
    app.start_download()
    pump(app, 0.2)
    assert app.stage == "checked"
    assert app.banner_status is Status.SAVE_FAILED


# ---- more options: clip, subtitles, project ------------------------------------------------------

def test_more_options_are_collapsed_until_opened(make_app):
    app = make_app()
    check(app, "https://www.youtube.com/watch?v=subs")
    pump(app, 0.2)
    assert not visible(app, "more")
    app.toggle_more()
    pump(app, 0.2)
    assert visible(app, "more")
    assert app.subs_menu.winfo_ismapped()
    assert app.subs_menu.cget("values") == ["No subtitles", "English", "English (auto-generated)"]


def test_clip_subtitles_and_project_reach_the_job(make_app, tmp_path):
    eng = FakeEngine()
    app = make_app(eng)
    check(app, "https://www.youtube.com/watch?v=subs")
    app.toggle_more()
    app.clip_var.set(True)
    app._on_clip_toggled()
    app.clip_start.insert(0, "0:10")
    app.clip_end.insert(0, "1:05")
    app.subs_menu.set("English (auto-generated)")
    app.project_box.set("Nike / Spring 2027")
    app._refresh_folder_label()
    pump(app, 0.1)
    assert "Spring 2027" in app.folder_label.cget("text")
    app.start_download()
    finish(app)
    job = eng.jobs[0]
    assert job.clip == (10.0, 65.0)
    assert job.subtitles.lang == "en-orig" and job.subtitles.auto
    assert job.folder == tmp_path / "out" / "Nike" / "Spring 2027"
    assert job.name_prefix.startswith("Nike_Spring-2027_")
    assert app.settings.recent_projects[0] == "Nike / Spring 2027"
    assert app.saved_settings  # recent projects were saved


def test_bad_clip_times_show_a_message(make_app):
    eng = FakeEngine()
    app = make_app(eng)
    check(app, "https://www.youtube.com/watch?v=1")
    app.toggle_more()
    app.clip_var.set(True)
    app.clip_start.insert(0, "1:00")
    app.clip_end.insert(0, "0:30")
    app.start_download()
    pump(app, 0.2)
    assert "must end after it starts" in app.banner_text.cget("text")
    assert eng.jobs == []


# ---- queue and batch paste ------------------------------------------------------------------------

def test_download_while_busy_goes_to_the_queue(make_app):
    eng = FakeEngine(step=0.1)  # 2 s per download, so the first is still running
    app = make_app(eng)
    check(app, "https://www.youtube.com/watch?v=1")
    app.start_download()
    pump(app, 0.2)
    assert app.stage == "downloading"
    # Paste the next link while the first downloads: the first moves to the Queue panel.
    check(app, "https://www.youtube.com/watch?v=2")
    pump(app, 0.1)
    assert visible(app, "queue")
    app.start_download()
    pump(app, 0.2)
    assert "Added to the queue" in app.banner_text.cget("text")
    assert app.stage == "empty"
    assert app.queue_title.cget("text") == "Queue (2)"
    finish(app, 15)
    assert not visible(app, "queue")
    assert len(app.history.entries) == 2
    assert all(e.ok for e in app.history.entries)


def test_batch_paste_queues_every_link(make_app, tmp_path):
    eng = FakeEngine(step=0.01)
    app = make_app(eng, settings=Settings(save_folder=str(tmp_path / "out"), default_format="wav"))
    app.accept_text("https://vimeo.com/1\nhttps://www.youtube.com/watch?v=2\nhttps://example.com/fail")
    pump(app, 0.2)
    assert "Added 3 links to the queue" in app.banner_text.cget("text")
    assert visible(app, "queue")
    finish(app, 10)
    assert [j.fmt.key for j in eng.jobs] == ["wav", "wav", "wav"]
    assert eng.jobs[0].login == ZEN and eng.jobs[1].login == NONE_SOURCE  # Vimeo uses the browser login
    oks = [e.ok for e in app.history.entries]
    assert sorted(oks) == [False, True, True]


def test_cancel_a_waiting_queue_item(make_app):
    eng = FakeEngine(step=0.05)
    app = make_app(eng)
    app.accept_text("https://example.com/a https://example.com/b")
    pump(app, 0.3)
    waiting = app.queue.waiting
    assert len(waiting) == 1
    app.queue.cancel(waiting[0].id)
    finish(app, 10)
    assert len(eng.jobs) == 1
    assert len(app.history.entries) == 1  # the cancelled one isn't recorded


# ---- history --------------------------------------------------------------------------------------

def test_history_rows_red_when_failed_and_click_redownloads(make_app, tmp_path):
    from app import theme
    eng = FakeEngine()
    app = make_app(eng)
    check(app, "https://www.youtube.com/watch?v=subs")
    app.format_menu.set(FORMATS_BY_KEY["prores"].label)
    app._on_format_changed()
    app.toggle_more()
    app.subs_menu.set("English")
    app.start_download()
    finish(app)
    check(app, "https://example.com/fail")
    app.start_download()
    finish(app)
    assert [e.ok for e in app.history.entries] == [False, True]
    rows = app.history_list.winfo_children()
    assert len(rows) == 2
    colors = [r.cget("fg_color") for r in rows]
    assert colors == [theme.ERROR_BG, "transparent"]  # only failures are colored
    assert all(r.winfo_height() <= 40 for r in rows)  # one compact line each
    assert visible(app, "history")

    # Click the green row: the link comes back with the same format and subtitles.
    app.download_another()
    pump(app, 0.2)
    import customtkinter as ctk
    rows = app.history_list.winfo_children()
    title = next(w for w in rows[1].winfo_children() if isinstance(w, ctk.CTkLabel))
    title._label.event_generate("<Button-1>")  # where a real click on the title lands
    assert pump(app, 3, lambda: app.stage == "checked")
    assert app.url_var.get() == "https://www.youtube.com/watch?v=subs"
    assert app.format_menu.get() == FORMATS_BY_KEY["prores"].label
    assert app.subs_menu.get() == "English"


def test_history_persists_and_can_be_cleared(make_app, tmp_path):
    h = History()  # real file in the test's VD_APPDATA
    app = make_app(FakeEngine(), history=h)
    check(app, "https://www.youtube.com/watch?v=1")
    app.start_download()
    finish(app)
    assert History().entries[0].title.startswith("Title for")
    app.clear_history(confirm=False)
    pump(app, 0.1)
    assert History().entries == []
    assert not visible(app, "history")


def test_history_entry_without_file_has_no_show_button(make_app):
    entry = HistoryEntry(url="https://example.com/x", title="Gone", ok=True, files=["/nope/x.mp4"])
    app = make_app(history=History([entry], persist=False))
    pump(app, 0.2)
    row = app.history_list.winfo_children()[0]
    buttons = [w for w in row.winfo_children() if w.winfo_class() == "Frame" and hasattr(w, "cget")
               and "Show" == getattr(w, "_text", None)]
    assert buttons == []


# ---- keyboard, clipboard, drag and drop -------------------------------------------------------------

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


def test_enter_checks_when_nothing_checked(make_app):
    eng = FakeEngine()
    app = make_app(eng)
    app.url_var.set("https://vimeo.com/7")
    press(app, "<Return>")
    assert pump(app, 3, lambda: app.stage == "checked")


def test_copied_link_is_used_when_the_app_gets_focus(make_app):
    eng = FakeEngine()
    app = make_app(eng)
    app.clipboard_clear()
    app.clipboard_append("https://www.youtube.com/watch?v=copied")  # copied while the app is open
    app._on_focus_in()
    assert pump(app, 3, lambda: app.stage == "checked")
    assert app.url_var.get() == "https://www.youtube.com/watch?v=copied"
    # The same clipboard content isn't used twice.
    app.download_another()
    app._on_focus_in()
    pump(app, 0.3)
    assert app.url_var.get() == ""


def test_link_already_on_clipboard_at_start_is_not_used(make_app):
    import tkinter
    root = tkinter.Tk()
    root.clipboard_clear()
    root.clipboard_append("https://www.youtube.com/watch?v=stale")
    root.update()
    app = make_app()
    app._on_focus_in()
    pump(app, 0.3)
    assert app.url_var.get() == "" and app.stage == "empty"
    root.destroy()


def test_copied_plain_text_is_ignored(make_app):
    app = make_app()
    app.clipboard_clear()
    app.clipboard_append("just some notes, not a link")
    app._on_focus_in()
    pump(app, 0.2)
    assert app.url_var.get() == "" and app.stage == "empty"


def test_dropping_a_link_checks_it(make_app):
    eng = FakeEngine()
    app = make_app(eng)

    class Drop:
        data = "https://vimeo.com/99"
        action = "copy"

    app._on_drop(Drop())
    assert pump(app, 3, lambda: app.stage == "checked")
    assert app.url_var.get() == "https://vimeo.com/99"


# ---- settings + persistence ------------------------------------------------------------------------

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
    win.format_menu.set(FORMATS_BY_KEY["prores"].label)
    win._on_login_selected(CHROME.label)
    win.auto_update_var.set(False)
    win.copied_var.set(False)
    win.save()
    pump(app, 0.2)
    [saved] = app.saved_settings
    assert saved.save_folder == str(new_folder)
    assert saved.default_quality == "1080"
    assert saved.default_format == "prores"
    assert saved.login_source == CHROME.key
    assert saved.check_updates_on_launch is False and saved.use_copied_links is False
    assert app.login_candidates() == [CHROME]


def test_settings_login_choices(make_app):
    app = make_app()
    app.open_settings()
    pump(app, 0.3)
    win = app._settings_win
    values = win.login_menu.cget("values")
    assert values[0] == "Automatic: try each browser (Zen, Chrome)"
    assert values[-1] == "Never use a browser login"
    win._on_login_selected("Never use a browser login")
    win.save()
    pump(app, 0.2)
    assert app.saved_settings[-1].login_source == "none"
    assert app.login_candidates() == []


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
    from app import settings as settings_mod, theme
    from app.ui_main import MainWindow
    monkeypatch.setenv("VD_APPDATA", str(tmp_path / "appdata"))
    folder = tmp_path / "Videos"
    folder.mkdir()
    theme.apply()
    app = MainWindow(settings_mod.load(), FakeEngine(), login_sources=[NONE_SOURCE], check_updates=False,
                     warm_up_engine=False)
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
    app2 = MainWindow(restarted, FakeEngine(), login_sources=[NONE_SOURCE], check_updates=False,
                      warm_up_engine=False)
    pump(app2, 0.3)
    assert str(folder) in app2.folder_label.cget("text") or "…" in app2.folder_label.cget("text")
    app2.destroy()
    import gc
    gc.collect()


def test_minimum_width(make_app):
    app = make_app()
    app.geometry("300x200")
    pump(app, 0.3)
    assert app.winfo_width() >= 500


# ---- thumbnails ------------------------------------------------------------------------------------

def _png_bytes(color=(200, 30, 30)) -> bytes:
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (320, 180), color).save(buf, "PNG")
    return buf.getvalue()


class ThumbEngine(FakeEngine):
    def check_link(self, req, on_attempt=None):
        res = super().check_link(req)
        if res.video:
            res.video.thumbnail = "https://example.invalid/thumb.png"
        return res


def test_thumbnail_then_new_check_does_not_crash(make_app, monkeypatch):
    """Regression (user log): after a thumbnail was shown, re-checking raised
    'image "pyimage1" doesn't exist' from _clear_result and on every later action."""
    monkeypatch.setattr("app.ui_main.fetch_thumbnail", lambda url: _png_bytes())
    errors = []
    app = make_app(ThumbEngine())
    app.report_callback_exception = lambda *exc: errors.append(exc)
    check(app, "https://www.youtube.com/watch?v=1")
    assert pump(app, 2, lambda: app._thumb_image is not None)
    check(app, "https://www.youtube.com/watch?v=2")
    pump(app, 0.5)
    app.retry()
    assert pump(app, 3, lambda: app.stage == "checked")
    app.url_var.set("https://www.youtube.com/watch?v=3")
    pump(app, 0.2)
    app.download_another()
    pump(app, 0.2)
    check(app, "https://www.youtube.com/watch?v=4")
    pump(app, 0.5)
    assert errors == []
    assert app.banner_status is Status.READY


# ---- real engine: the UI never freezes ---------------------------------------------------------------

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


def test_real_engine_prores_through_the_ui(make_app, media_server, tmp_path):
    """The whole path with the real engine: check, pick ProRes, download, convert, history."""
    from app.engine import Engine
    app = make_app(Engine())
    check(app, f"{media_server}/dash/manifest.mpd", timeout=30)
    app.format_menu.set(FORMATS_BY_KEY["prores"].label)
    app._on_format_changed()
    app.start_download()
    assert pump(app, 60, lambda: app.stage == "done"), app.progress_text.cget("text")
    [f] = app.saved_files
    assert f.suffix == ".mov" and f.stat().st_size > 0
    assert app.history.entries[0].ok and app.history.entries[0].format_key == "prores"
