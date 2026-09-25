"""Turn yt-dlp exceptions and error text into plain-English states.

Everything the user sees about a failure comes from ``classify_error``. The raw
error text is kept separately (``ErrorInfo.detail``) and is only ever shown in
the collapsible "Details" box.

This module deliberately does not import yt_dlp: exceptions are recognised by
class name and message text so it can be imported (and tested) before the
engine is loaded.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass


class Status(enum.Enum):
    READY = "ready"
    NEEDS_LOGIN = "needs_login"
    NEEDS_PASSWORD = "needs_password"
    EMBED_RESTRICTED = "embed_restricted"
    COOKIES_LOCKED = "cookies_locked"          # browser holds its cookie database / login looks stale
    COOKIES_UNREADABLE = "cookies_unreadable"  # Chrome/Edge/Brave encryption blocked the read
    COOKIES_MISSING = "cookies_missing"        # no cookie database in the chosen browser/profile
    DRM = "drm"
    INVALID_URL = "invalid_url"
    UNSUPPORTED = "unsupported"                # unsupported site, or no video on the page
    REMOVED = "removed"                        # removed, unavailable, not started yet, 404
    GEO_BLOCKED = "geo_blocked"
    NETWORK = "network"
    RATE_LIMITED = "rate_limited"
    SAVE_FAILED = "save_failed"                # disk full, folder not writable
    NEEDS_UPDATE = "needs_update"              # the site changed; a newer engine usually fixes it
    APP_PROBLEM = "app_problem"                # a bundled component (FFmpeg) is missing
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


class Severity(enum.Enum):
    OK = "ok"        # green
    WARN = "warn"    # yellow: the user can do something about it
    ERROR = "error"  # red: nothing to do (or only "try again")


SEVERITY = {
    Status.READY: Severity.OK,
    Status.NEEDS_LOGIN: Severity.WARN,
    Status.NEEDS_PASSWORD: Severity.WARN,
    Status.EMBED_RESTRICTED: Severity.WARN,
    Status.COOKIES_LOCKED: Severity.WARN,
    Status.COOKIES_UNREADABLE: Severity.WARN,
    Status.COOKIES_MISSING: Severity.WARN,
    Status.CANCELLED: Severity.WARN,
}


def severity_of(status: Status) -> Severity:
    return SEVERITY.get(status, Severity.ERROR)


@dataclass(frozen=True)
class ErrorInfo:
    status: Status
    message: str        # one plain-English sentence (or two) for the banner
    detail: str = ""    # raw error text for the Details box

    @property
    def severity(self) -> Severity:
        return severity_of(self.status)


FIREFOX_FAMILY_HINT = "Firefox, Zen, LibreWolf or Floorp"
LOGIN_HELP = (
    f"This video is private or members-only. Log into the site in your browser ({FIREFOX_FAMILY_HINT}), "
    "then click Retry."
)

MESSAGES = {
    Status.NEEDS_LOGIN: LOGIN_HELP,
    Status.NEEDS_PASSWORD: (
        "This video needs a password. If you were given the password, enter it below and click Retry."
    ),
    Status.EMBED_RESTRICTED: (
        "This video only plays on a specific website. Enter the address of the page it plays on, "
        "then click Retry."
    ),
    Status.COOKIES_LOCKED: (
        "Couldn't read your login from {browser}. Close {browser} completely, then click Retry."
    ),
    Status.COOKIES_UNREADABLE: (
        "Windows security usually blocks reading logins from {browser}. For private videos, log in "
        f"with {FIREFOX_FAMILY_HINT} and choose it in Settings under 'Browser for logins'."
    ),
    Status.COOKIES_MISSING: (
        "Couldn't find a saved login in {browser}. Open it, log into the site, then click Retry."
    ),
    Status.DRM: "This video is copy-protected and can't be downloaded.",
    Status.INVALID_URL: (
        "That doesn't look like a web address. Copy the link from your browser's address bar and paste it here."
    ),
    Status.UNSUPPORTED: (
        "Couldn't find a video at this link. The page may not have a video, or this website isn't supported."
    ),
    Status.REMOVED: "This video isn't available. It may have been removed by its owner.",
    Status.GEO_BLOCKED: "This video isn't available in your country.",
    Status.NETWORK: "Couldn't connect. Check your internet connection and try again.",
    Status.RATE_LIMITED: "The website is limiting downloads right now. Wait a few minutes, then try again.",
    Status.SAVE_FAILED: "Couldn't save the file. Check that the folder exists and the disk isn't full.",
    Status.NEEDS_UPDATE: (
        "Couldn't read this page. The website may have changed. Update the downloader engine in "
        "Settings, then try again."
    ),
    Status.APP_PROBLEM: "Part of the app is missing. Download the app again, then retry.",
    Status.CANCELLED: "Download cancelled.",
    Status.UNKNOWN: (
        "Something went wrong. Try again, or update the downloader engine in Settings. "
        "Details has more information."
    ),
}


# Ordered (status, patterns) rules. The first rule with a matching pattern wins,
# so more specific / more definitive states come first. Patterns are matched
# case-insensitively against every message in the exception chain.
_RULES: list[tuple[Status, tuple[str, ...]]] = [
    # DRM is definitive: nothing else matters and there is no workaround.
    (Status.DRM, (
        r"\bdrm\b",
        r"widevine",
        r"playready",
        r"fairplay",
    )),
    # Problems reading the browser's login come before anything that might look
    # like a login/network problem, because the fix is different.
    (Status.COOKIES_UNREADABLE, (
        r"failed to decrypt with dpapi",
        r"could not copy chrome cookie database",
        r"app[- ]bound",
        r"cannot decrypt v\d\d cookies",
        r"failed to decrypt",
        r"could not find local state file",
    )),
    (Status.COOKIES_LOCKED, (
        r"database is locked",
        r"unable to open database file",
        r"cookies?\.sqlite.*permission denied",
        r"permission denied.*cookies?",
    )),
    (Status.COOKIES_MISSING, (
        r"could not find \w+ cookies database",
        r"could not find .* cookies",
        r"custom safari cookies database not found",
    )),
    (Status.NEEDS_PASSWORD, (
        r"video-password",
        r"wrong (video )?password",
        r"invalid password",
        r"password[- ]protected",
        r"protected by a (password|passcode)",
    )),
    (Status.EMBED_RESTRICTED, (
        r"embed-only video",
        r"cannot download embed-only",
        r"url of the page that embeds",
        r"only (be )?(played|embedded|available) on (certain|specific) (sites|websites|domains)",
        r"privacy settings.*embed",
    )),
    (Status.GEO_BLOCKED, (
        r"not available from your location",
        r"not available in your (country|region)",
        r"geo[- ]?restrict",
        r"blocked it in your country",
        r"uploader has not made this video available in your country",
    )),
    (Status.NEEDS_LOGIN, (
        r"only available for registered users",
        r"only works when logged-?in",
        r"login required",
        r"log ?in (is )?required",
        r"requires? (a )?(login|authentication|subscription)",
        r"private video",
        r"this video is private",
        r"members[- ]only",
        r"join this channel",
        r"sign in to confirm",
        r"sign in if you've been granted access",
        r"--cookies-from-browser",
        r"account credentials",
        r"use --cookies",
        r"http error 401",
        r"not authorized",
        r"authentication",
    )),
    (Status.APP_PROBLEM, (
        r"ffmpeg not found",
        r"ffprobe (and ffmpeg )?not found",
        r"ffmpeg is not installed",
        r"ffprobe/avprobe and ffmpeg/avconv not found",
    )),
    (Status.INVALID_URL, (
        r"is not a valid url",
        r"invalid url",
        r"unknown url type",
        r"no connection adapters",
    )),
    (Status.UNSUPPORTED, (
        r"unsupported url",
        r"no video formats found",
        r"no video could be found",
        r"no media found",
        r"there's no video in this",
    )),
    (Status.NEEDS_UPDATE, (
        r"unable to extract",
        r"signature extraction failed",
        r"nsig extraction failed",
        r"please report this issue",
        r"update to the latest version",
        r"--update",
    )),
    (Status.REMOVED, (
        r"video unavailable",
        r"has been removed",
        r"no longer available",
        r"account .* (has been )?terminated",
        r"this video (is|has been) (deleted|removed|unavailable)",
        r"http error 404",
        r"http error 410",
        r"not found",
        r"does not exist",
        r"this video is not available",
        r"is no longer available",
        r"live event will begin",
        r"premieres in",
        r"this live event has ended",
    )),
    (Status.RATE_LIMITED, (
        r"http error 429",
        r"too many requests",
        r"rate[- ]limit",
    )),
    (Status.SAVE_FAILED, (
        r"no space left on device",
        r"errno 28",
        r"disk (is )?full",
        r"there is not enough space",
        r"unable to (open|create|write|rename) file",
        r"read-only file system",
    )),
    (Status.NETWORK, (
        r"unable to download (webpage|json|api|xml)",
        r"urlopen error",
        r"getaddrinfo failed",
        r"name or service not known",
        r"temporary failure in name resolution",
        r"nodename nor servname",
        r"timed? ?out",
        r"connection (reset|refused|aborted)",
        r"remote end closed connection",
        r"network is unreachable",
        r"no route to host",
        r"ssl",
        r"certificate verify failed",
        r"http error 5\d\d",
        r"incompleteread",
        r"connectionerror",
        r"transporterror",
        r"proxyerror",
    )),
]

_COMPILED = [(status, re.compile("|".join(f"(?:{p})" for p in pats), re.IGNORECASE)) for status, pats in _RULES]

# Exception class names that pin down a state regardless of message text.
_TYPE_RULES = {
    "UnsupportedError": Status.UNSUPPORTED,
    "GeoRestrictedError": Status.GEO_BLOCKED,
    "DownloadCancelled": Status.CANCELLED,
    "UserCancelled": Status.CANCELLED,
    "TransportError": Status.NETWORK,
    "HTTPError": None,  # decided by status code in the text
}

_NETWORK_TYPES = (ConnectionError, TimeoutError)


def _exception_chain(exc: BaseException) -> list[BaseException]:
    """All exceptions reachable from ``exc`` via yt-dlp's ``exc_info`` and Python's cause/context links."""
    seen: list[BaseException] = []
    stack = [exc]
    while stack:
        cur = stack.pop()
        if cur is None or any(cur is s for s in seen):
            continue
        seen.append(cur)
        exc_info = getattr(cur, "exc_info", None)
        if isinstance(exc_info, tuple) and len(exc_info) > 1 and isinstance(exc_info[1], BaseException):
            stack.append(exc_info[1])
        cause = getattr(cur, "cause", None)
        if isinstance(cause, BaseException):
            stack.append(cause)
        stack.append(cur.__cause__)
        stack.append(cur.__context__)
    return seen


def _texts(chain: list[BaseException]) -> list[str]:
    out = []
    for e in chain:
        for text in (str(e), getattr(e, "orig_msg", None), getattr(e, "msg", None)):
            if isinstance(text, str) and text and text not in out:
                out.append(text)
    return out


def _strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def classify_text(text: str) -> Status | None:
    """Return the first matching state for a single error message, or None."""
    for status, rx in _COMPILED:
        if rx.search(text):
            return status
    return None


def classify_error(exc: BaseException | str, *, browser: str | None = None,
                   password_given: bool = False, referer_given: bool = False) -> ErrorInfo:
    """Map an exception (or raw error text) from yt-dlp to an :class:`ErrorInfo`.

    ``browser`` is the display name of the login source in use (if any); it is
    used in the cookie-related messages and to tell "needs login" apart from
    "the login we used didn't work".
    """
    if isinstance(exc, str):
        chain: list[BaseException] = []
        texts = [exc]
    else:
        chain = _exception_chain(exc)
        texts = _texts(chain)
    detail = _strip_ansi("\n".join(texts)).strip()

    status: Status | None = None
    # Message text first: it is the most specific signal yt-dlp gives us.
    combined = "\n".join(texts)
    status = classify_text(combined)

    if status is None:
        for e in chain:
            name_status = _TYPE_RULES.get(type(e).__name__)
            if name_status is not None:
                status = name_status
                break
            if isinstance(e, _NETWORK_TYPES):
                status = Status.NETWORK
                break
            if isinstance(e, PermissionError):
                status = Status.SAVE_FAILED
                break
            if isinstance(e, OSError) and getattr(e, "errno", None) == 28:
                status = Status.SAVE_FAILED
                break

    # A cancellation anywhere in the chain wins: it was the user's choice.
    if any(type(e).__name__ in ("DownloadCancelled", "UserCancelled") for e in chain):
        status = Status.CANCELLED

    if status is None:
        status = Status.UNKNOWN

    return ErrorInfo(status, _message_for(status, browser, password_given, referer_given, combined), detail)


def _message_for(status: Status, browser: str | None, password_given: bool,
                 referer_given: bool, text: str) -> str:
    who = browser or "your browser"
    low = text.lower()
    if status is Status.NEEDS_LOGIN:
        if browser:
            return (f"The login from {browser} didn't work. Make sure you're logged into the site in "
                    f"{browser}, close {browser} completely, then click Retry.")
        if ("confirm you" in low and "bot" in low) or "only works when logged-in" in low:
            # The site asks for a login even for public videos (bot checks, some networks).
            return (f"The site wants you to sign in first. Log into it in your browser ({FIREFOX_FAMILY_HINT}), "
                    "then click Retry.")
        if "confirm your age" in low or "age-restricted" in low or "age restricted" in low:
            return (f"This video is age-restricted. Log into the site in your browser ({FIREFOX_FAMILY_HINT}), "
                    "then click Retry.")
        return MESSAGES[status]
    if status is Status.NEEDS_PASSWORD and password_given:
        return "That password didn't work. Check it and click Retry."
    if status is Status.EMBED_RESTRICTED and referer_given:
        return ("The video still won't play from that page. Check the address of the page it plays on, "
                "then click Retry.")
    if status is Status.REMOVED:
        if "live event will begin" in low or "premieres in" in low:
            return "This video hasn't started yet. Try again once it has aired."
        if "live event has ended" in low:
            return "This live stream has ended and isn't available yet. Try again later."
    return MESSAGES[status].format(browser=who)


def ready() -> ErrorInfo:
    return ErrorInfo(Status.READY, "Ready to download.")
