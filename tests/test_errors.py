import sqlite3

import pytest
from yt_dlp.cookies import CookieLoadError
from yt_dlp.networking.exceptions import HTTPError, TransportError
from yt_dlp.utils import (
    DownloadCancelled,
    DownloadError,
    ExtractorError,
    GeoRestrictedError,
    UnsupportedError,
)

from app import errors
from app.errors import ErrorInfo, Severity, Status, classify_error, classify_text, login_attempts_failed


def wrap(exc: BaseException) -> DownloadError:
    """Wrap an exception the way YoutubeDL.extract_info does before re-raising."""
    try:
        raise exc
    except BaseException as inner:  # noqa: BLE001
        import sys
        return DownloadError(f"ERROR: {inner}", sys.exc_info())


def cookie_load_error(cause: BaseException) -> CookieLoadError:
    """Build the CookieLoadError yt-dlp raises, with the real cause as __context__."""
    try:
        try:
            raise cause
        except BaseException:  # noqa: BLE001
            raise CookieLoadError("failed to load cookies")
    except CookieLoadError as e:
        return e


# ---- one test per state ---------------------------------------------------------------------

def test_drm():
    info = classify_error(wrap(ExtractorError("This video is DRM protected", expected=True)))
    assert info.status is Status.DRM
    assert info.severity is Severity.ERROR
    assert "copy-protected" in info.message


def test_drm_wins_over_login_text():
    text = "This video is DRM protected. Use --cookies-from-browser or --cookies for the authentication."
    assert classify_error(text).status is Status.DRM


@pytest.mark.parametrize("msg", [
    "This video is only available for registered users. Use --cookies-from-browser or --cookies "
    "for the authentication. See  https://github.com/yt-dlp/yt-dlp/wiki/FAQ  for how to manually pass cookies",
    "[youtube] abc: Private video. Sign in if you've been granted access to this video",
    "[youtube] abc: Join this channel to get access to members-only content like this video",
    "[youtube] abc: Sign in to confirm your age. This video may be inappropriate for some users.",
    "[youtube] abc: Sign in to confirm you're not a bot. Use --cookies-from-browser",
    "[vimeo] 123: HTTP Error 401: Unauthorized",
])
def test_needs_login(msg):
    info = classify_error(wrap(ExtractorError(msg, expected=True)))
    assert info.status is Status.NEEDS_LOGIN
    assert info.severity is Severity.WARN
    assert "--cookies" not in info.message  # never leak yt-dlp jargon


@pytest.fixture
def on_windows(monkeypatch):
    monkeypatch.setattr(errors.sys, "platform", "win32")


@pytest.fixture
def on_mac(monkeypatch):
    monkeypatch.setattr(errors.sys, "platform", "darwin")


def test_needs_login_message_when_no_browser(on_windows):
    info = classify_error("This video is only available for registered users")
    assert "Retry" in info.message and "Zen" in info.message  # on Windows only Firefox-family logins work
    assert "Use login from" not in info.message  # that dropdown no longer exists


def test_needs_login_message_on_mac_names_no_browser(on_mac):
    info = classify_error("This video is only available for registered users")
    assert info.message == "This video is private or members-only. Log into the site in your browser, then click Retry."


def test_needs_login_with_browser_selected_suggests_closing_browser():
    info = classify_error("This video is only available for registered users", browser="Zen — Default")
    assert info.status is Status.NEEDS_LOGIN
    assert "close Zen — Default" in info.message


def test_age_restricted_message():
    info = classify_error("Sign in to confirm your age. This video may be inappropriate")
    assert "age-restricted" in info.message


@pytest.mark.parametrize("msg", [
    "This video is protected by a password, use the --video-password option",
    "Wrong password",
    "Wrong video password",
    "This video is password-protected, use the --video-password option",
])
def test_needs_password(msg):
    info = classify_error(wrap(ExtractorError(msg, expected=True)))
    assert info.status is Status.NEEDS_PASSWORD
    assert "--video-password" not in info.message


def test_wrong_password_message():
    info = classify_error("Wrong video password", password_given=True)
    assert info.status is Status.NEEDS_PASSWORD
    assert "didn't work" in info.message


def test_embed_restricted():
    msg = ("Cannot download embed-only video without embedding URL. Please call yt-dlp with the URL "
           "of the page that embeds this video.")
    info = classify_error(wrap(ExtractorError(msg, expected=True)))
    assert info.status is Status.EMBED_RESTRICTED
    assert info.severity is Severity.WARN
    assert "specific website" in info.message


def test_embed_restricted_with_referer_given():
    info = classify_error("Cannot download embed-only video without embedding URL", referer_given=True)
    assert "still won't play" in info.message


def test_unsupported_by_type():
    info = classify_error(wrap(UnsupportedError("https://example.com/")))
    assert info.status is Status.UNSUPPORTED
    assert info.severity is Severity.ERROR


def test_no_formats_is_unsupported():
    assert classify_error("No video formats found!").status is Status.UNSUPPORTED


def test_invalid_url():
    assert classify_error(wrap(ExtractorError("'hello world' is not a valid URL", expected=True))).status \
        is Status.INVALID_URL


@pytest.mark.parametrize("msg", [
    "[youtube] abc: Video unavailable. This video has been removed by the uploader",
    "This video is no longer available because the YouTube account associated with this video has been terminated.",
    "Unable to download webpage: HTTP Error 404: Not Found",
    "[youtube] abc: This live event will begin in 3 hours.",
])
def test_removed(msg):
    assert classify_error(msg).status is Status.REMOVED


def test_not_started_message():
    assert "hasn't started" in classify_error("This live event will begin in 3 hours.").message


def test_geo_blocked_by_type():
    info = classify_error(wrap(GeoRestrictedError("This video is not available from your location due to geo restriction")))
    assert info.status is Status.GEO_BLOCKED


@pytest.mark.parametrize("msg", [
    "Unable to download webpage: <urlopen error [Errno 11001] getaddrinfo failed>",
    "Unable to download webpage: The read operation timed out",
    "Unable to download JSON metadata: [Errno 104] Connection reset by peer",
    "HTTP Error 503: Service Unavailable",
])
def test_network(msg):
    info = classify_error(wrap(ExtractorError(msg, expected=True)))
    assert info.status is Status.NETWORK
    assert "internet connection" in info.message


def test_network_by_exception_type():
    assert classify_error(TransportError("boom")).status is Status.NETWORK
    assert classify_error(ConnectionResetError()).status is Status.NETWORK


def test_rate_limited():
    assert classify_error("HTTP Error 429: Too Many Requests").status is Status.RATE_LIMITED


def test_http_error_object_404():
    class FakeResponse:
        status = 404
        reason = "Not Found"
        headers = {}
        url = "https://example.com"

        def read(self, *a):
            return b""

        def close(self):
            pass

    err = HTTPError(FakeResponse())
    assert classify_error(wrap(err)).status is Status.REMOVED


def test_cookies_locked_from_sqlite_chain():
    err = cookie_load_error(sqlite3.OperationalError("database is locked"))
    info = classify_error(err, browser="Firefox — default-release")
    assert info.status is Status.COOKIES_LOCKED
    assert "Close Firefox — default-release completely" in info.message


def test_cookies_unreadable_chrome(on_windows):
    err = cookie_load_error(DownloadError(
        "Failed to decrypt with DPAPI. See  https://github.com/yt-dlp/yt-dlp/issues/10927  for more info"))
    info = classify_error(err, browser="Chrome")
    assert info.status is Status.COOKIES_UNREADABLE
    assert "Windows blocks reading logins from Chrome" in info.message and "Firefox" in info.message


def test_cookies_unreadable_on_mac(on_mac):
    err = cookie_load_error(PermissionError(1, "Operation not permitted",
                                            "/Users/a/Library/Cookies/Cookies.binarycookies"))
    info = classify_error(err, browser="Safari")
    assert info.status is Status.COOKIES_UNREADABLE
    assert "Full Disk Access" in info.message
    info = classify_error(cookie_load_error(ValueError("unknown browser: arc")), browser="Arc")
    assert info.status is Status.COOKIES_UNREADABLE and "Always Allow" in info.message


def test_specific_cookie_problems_beat_the_generic_one():
    """"failed to load cookies" is in every cookie error; the specific cause still decides."""
    locked = cookie_load_error(sqlite3.OperationalError("database is locked"))
    assert classify_error(locked, browser="Zen").status is Status.COOKIES_LOCKED
    missing = cookie_load_error(FileNotFoundError("could not find chrome cookies database in 'x'"))
    assert classify_error(missing, browser="Chrome").status is Status.COOKIES_MISSING


# ---- trying each browser: one message when none worked --------------------------------------

def _needs_login(browser=None):
    return classify_error("This video is only available for registered users", browser=browser)


def _unreadable(browser):
    return classify_error("Failed to decrypt with DPAPI", browser=browser)


def test_all_browsers_failed_message(on_windows):
    info = login_attempts_failed([(None, _needs_login()), ("Zen", _needs_login("Zen")),
                                  ("Chrome", _unreadable("Chrome")), ("Edge", _unreadable("Edge"))])
    assert info.status is Status.NEEDS_LOGIN
    assert info.message == ("The site wants you to sign in first. Your login from Zen didn't work for it. "
                            "Windows blocks logins from Chrome and Edge. Log into the site in Zen, make sure the "
                            "video plays, then click Retry.")
    assert "Without a login: needs_login" in info.detail and "Chrome: cookies_unreadable" in info.detail


def test_only_chrome_on_windows(on_windows):
    info = login_attempts_failed([(None, _needs_login()), ("Chrome", _unreadable("Chrome")),
                                  ("Arc", _unreadable("Arc"))])
    assert info.message == ("The site wants you to sign in first. Windows blocks logins from Chrome and Arc. "
                            "Log into the site in your browser (Firefox, Zen, LibreWolf or Floorp), then click Retry.")


def test_several_logins_tried_on_mac(on_mac):
    locked = classify_error("database is locked", browser="Zen")
    info = login_attempts_failed([(None, _needs_login()), ("Chrome", _needs_login("Chrome")),
                                  ("Arc", _needs_login("Arc")), ("Zen", locked)])
    assert info.message == ("The site wants you to sign in first. Your logins from Chrome and Arc didn't work "
                            "for it. Close Zen completely so its login can be read. Log into the site in one of "
                            "them, make sure the video plays, then click Retry.")


def test_one_browser_keeps_its_own_message():
    locked = classify_error("database is locked", browser="Zen")
    info = login_attempts_failed([("Zen", locked), (None, _needs_login())])
    assert info.status is Status.COOKIES_LOCKED and info.message == locked.message


def test_cookies_unreadable_copy_failure():
    info = classify_error("Could not copy Chrome cookie database. See  https://github.com/...")
    assert info.status is Status.COOKIES_UNREADABLE


def test_cookies_missing():
    err = cookie_load_error(FileNotFoundError("could not find firefox cookies database in 'C:\\x'"))
    assert classify_error(err, browser="Zen").status is Status.COOKIES_MISSING


def test_save_failed():
    assert classify_error(OSError(28, "No space left on device")).status is Status.SAVE_FAILED
    assert classify_error(PermissionError(13, "Permission denied", "C:\\x.mp4")).status is Status.SAVE_FAILED


def test_ffmpeg_missing_is_app_problem():
    assert classify_error("ERROR: ffmpeg not found. Please install or provide the path").status \
        is Status.APP_PROBLEM


def test_needs_update():
    msg = ("[generic] Unable to extract title; please report this issue on  https://github.com/yt-dlp/yt-dlp/issues")
    assert classify_error(msg).status is Status.NEEDS_UPDATE


def test_cancelled():
    info = classify_error(DownloadCancelled("user cancelled"))
    assert info.status is Status.CANCELLED


def test_unknown_keeps_raw_detail():
    info = classify_error(RuntimeError("kaboom 42"))
    assert info.status is Status.UNKNOWN
    assert "kaboom 42" in info.detail
    assert "kaboom" not in info.message


def test_detail_strips_ansi():
    info = classify_error("\x1b[0;31mERROR:\x1b[0m Private video")
    assert "\x1b" not in info.detail


def test_every_state_has_a_message():
    from app.errors import MESSAGES
    for status in Status:
        if status is Status.READY:
            continue
        assert status in MESSAGES, status


def test_classify_text_none_for_harmless():
    assert classify_text("all good") is None


def test_errorinfo_is_frozen():
    info = ErrorInfo(Status.UNKNOWN, "x")
    with pytest.raises(Exception):
        info.status = Status.DRM  # type: ignore[misc]


def test_vimeo_login_only_client_uses_neutral_sign_in_message():
    msg = ("[vimeo] 76979871: The web client only works when logged-in. Use --cookies, --cookies-from-browser, "
           "--username and --password, --netrc-cmd, or --netrc (vimeo) to provide account credentials.")
    info = classify_error(msg)
    assert info.status is Status.NEEDS_LOGIN
    assert info.message.startswith("The site wants you to sign in first")
