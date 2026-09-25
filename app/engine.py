"""Thin wrapper around ``yt_dlp.YoutubeDL``: check a link, list qualities, download, cancel.

All functions here are blocking and are meant to be called from a worker
thread. They never touch the UI; progress is reported through a callback.

DRM: this module never enables ``allow_unplayable_formats`` and stops with a
"copy-protected" state whenever yt-dlp reports DRM. There is no workaround.
"""

from __future__ import annotations

import copy
import dataclasses
import gc
import logging
import os
import re
import subprocess
import threading
import time
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlparse

import yt_dlp
from yt_dlp.utils import DownloadCancelled

from . import paths
from .browsers import NONE_SOURCE, LoginSource
from .errors import ErrorInfo, Status, classify_error, login_attempts_failed, ready

log = logging.getLogger(__name__)

MP3_KBPS = 192
# Words users see next to heights.
HEIGHT_NAMES = {2160: "4K", 1440: "2K", 720: "HD", 1080: "Full HD", 4320: "8K"}
PARTIAL_SUFFIXES = (".part", ".ytdl", ".temp", ".tmp")


def engine_version() -> str:
    return yt_dlp.version.__version__


# --------------------------------------------------------------------------------------------
# URL handling
# --------------------------------------------------------------------------------------------

_SCHEME_RX = re.compile(r"^[a-z][a-z0-9+.\-]*://", re.IGNORECASE)
_BARE_HOST_RX = re.compile(r"^(?:[\w\-]+\.)+[a-z]{2,}(?::\d+)?(?:[/?#].*)?$", re.IGNORECASE)


def normalize_url(text: str) -> str | None:
    """Return a usable http(s) URL, or None if ``text`` isn't a web address.

    Accepts bare addresses like ``youtube.com/watch?v=...`` by adding ``https://``.
    """
    t = (text or "").strip().strip("<>\"'")
    if not t or any(c.isspace() for c in t):
        return None
    if not _SCHEME_RX.match(t):
        if not _BARE_HOST_RX.match(t):
            return None
        t = "https://" + t
    parsed = urlparse(t)
    if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
        return None
    host = parsed.hostname
    if "." not in host and host != "localhost":
        return None
    return t


def _looks_like_playlist_url(url: str) -> bool:
    q = parse_qs(urlparse(url).query)
    return "list" in q


# --------------------------------------------------------------------------------------------
# Data passed to the UI
# --------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Quality:
    key: str                 # "best", "h1080"
    label: str               # "1080p (Full HD)"
    height: int | None = None
    size: int | None = None  # approximate bytes, if known

    @property
    def display(self) -> str:
        return f"{self.label} · ~{human_size(self.size)}" if self.size else self.label


BEST = Quality("best", "Best available")


@dataclass(frozen=True)
class OutputFormat:
    key: str
    label: str
    ext: str
    kind: str                 # "video", "audio" or "image"
    convert: str | None = None  # re-encode after download: "edit" or "prores"

    @property
    def uses_quality(self) -> bool:
        return self.kind == "video"


FORMATS = (
    OutputFormat("original", "Original MP4", "mp4", "video"),
    OutputFormat("edit", "Edit-ready MP4", "mp4", "video", convert="edit"),
    OutputFormat("prores", "ProRes 422 HQ", "mov", "video", convert="prores"),
    OutputFormat("mp3", "MP3 audio", "mp3", "audio"),
    OutputFormat("wav", "WAV audio", "wav", "audio"),
    OutputFormat("jpg", "Thumbnail (JPG)", "jpg", "image"),
)
FORMATS_BY_KEY = {f.key: f for f in FORMATS}
ORIGINAL = FORMATS_BY_KEY["original"]


def format_by_key(key: str | None) -> OutputFormat:
    return FORMATS_BY_KEY.get(key or "", ORIGINAL)


@dataclass(frozen=True)
class SubtitleChoice:
    lang: str                 # yt-dlp language code, e.g. "en" or "en-orig"
    label: str                # "English" / "English (auto-generated)"
    auto: bool = False


def format_opts(quality: Quality | None, fmt: OutputFormat) -> dict:
    """yt-dlp format selection for a quality + output format.

    Video prefers H.264 + AAC/M4A in MP4 (merged, or remuxed if the source is a single WebM).
    """
    if fmt.kind == "audio":
        return {
            "format": "ba/b",
            "format_sort": ["acodec:aac", "abr"],
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": fmt.ext,
                "preferredquality": str(MP3_KBPS) if fmt.ext == "mp3" else "0",
            }],
        }
    if fmt.kind == "image":
        return {
            "skip_download": True,
            "writethumbnail": True,
            "postprocessors": [{"key": "FFmpegThumbnailsConvertor", "format": "jpg", "when": "before_dl"}],
        }
    height = quality.height if quality else None
    res = f"res:{height}" if height else "res"
    return {
        "format": "bv*+ba/b",
        "format_sort": [res, "vcodec:h264", "acodec:aac", "ext:mp4:m4a"],
        "merge_output_format": "mp4",
        "postprocessors": [{"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"}],
    }


# --------------------------------------------------------------------------------------------
# Clip ranges, project names, batch links
# --------------------------------------------------------------------------------------------

_TIME_RX = re.compile(r"^\s*(?:(\d+):)?(?:(\d+):)?(\d+(?:[.,]\d+)?)\s*$")


def parse_timecode(text: str) -> float | None:
    """'83', '1:23', '0:01:23.5' -> seconds. None if empty or not a time."""
    m = _TIME_RX.match(text or "")
    if not m:
        return None
    a, b, sec = m.groups()
    parts = [p for p in (a, b) if p is not None]
    total = float(sec.replace(",", "."))
    for i, p in enumerate(reversed(parts)):
        total += int(p) * (60 ** (i + 1))
    return total


def format_timecode(seconds: float) -> str:
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    frac = seconds - int(seconds)
    tail = f"{s:02d}" + (f".{int(round(frac * 10))}" if frac >= 0.05 else "")
    return f"{h}:{m:02d}:{tail}" if h else f"{m}:{tail}"


def clip_suffix(clip: tuple[float, float] | None) -> str:
    """Windows-safe file name suffix for a clip, e.g. ' (clip 0m42s-0m58s)'."""
    if not clip:
        return ""

    def t(x: float) -> str:
        s = int(x)
        h, rem = divmod(s, 3600)
        m, s = divmod(rem, 60)
        return f"{h}h{m:02d}m{s:02d}s" if h else f"{m}m{s:02d}s"

    return f" (clip {t(clip[0])}-{t(clip[1])})"


def project_parts(text: str | None) -> list[str]:
    """'Nike / Spring 2027' -> ['Nike', 'Spring 2027'] (Windows-safe, empty parts dropped)."""
    if not text:
        return []
    return [safe_folder_name(p.strip()) for p in re.split(r"[/\\]", text) if p.strip()]


def project_prefix(text: str | None, today: str | None = None) -> str:
    """File name prefix for a project: 'Nike_Spring-2027_2026-09-25_'."""
    parts = project_parts(text)
    if not parts:
        return ""
    import datetime
    today = today or datetime.date.today().isoformat()
    return "_".join(p.replace(" ", "-") for p in parts) + f"_{today}_"


_URL_IN_TEXT_RX = re.compile(r"(?:https?://|www\.)[^\s<>\"']+", re.IGNORECASE)


def extract_urls(text: str) -> list[str]:
    """All distinct web links in pasted text (one or many), in order."""
    seen, out = set(), []
    for raw in _URL_IN_TEXT_RX.findall(text or ""):
        url = normalize_url(raw.rstrip(".,);]"))
        if url and url not in seen:
            seen.add(url)
            out.append(url)
    return out


@dataclass
class VideoInfo:
    title: str
    uploader: str | None = None
    duration: float | None = None
    thumbnail: str | None = None
    webpage_url: str | None = None
    site: str | None = None


@dataclass
class PlaylistInfo:
    title: str
    count: int
    entry_urls: list[str]
    # True when the link is only a playlist (no "current" video); then "just this
    # video" means the first one.
    pure: bool


@dataclass
class CheckRequest:
    url: str
    login: LoginSource = NONE_SOURCE
    password: str | None = None
    referer: str | None = None
    # If ``login`` doesn't work (the site asks for a login, or the browser's login can't be
    # read), these are tried in order. NONE_SOURCE in the list means "try without a login".
    fallback_logins: tuple[LoginSource, ...] = ()


@dataclass
class CheckResult:
    result: ErrorInfo
    url: str | None = None                 # normalized URL of the single video
    video: VideoInfo | None = None
    qualities: list[Quality] = field(default_factory=list)
    playlist: PlaylistInfo | None = None
    subtitles: list[SubtitleChoice] = field(default_factory=list)
    has_video: bool = True
    login: LoginSource = NONE_SOURCE       # the browser login that worked (download with this one)
    # (login key, result) for each login tried that didn't work, in order.
    failed_logins: tuple[tuple[str, Status], ...] = ()

    @property
    def used_login(self) -> bool:
        return not self.login.is_none

    @property
    def ok(self) -> bool:
        return self.result.status is Status.READY

    @property
    def formats(self) -> list[OutputFormat]:
        """Output formats this source supports."""
        return [f for f in FORMATS if self.has_video or f.kind != "video"]


@dataclass
class Progress:
    percent: float | None      # 0..100 for the current video, None if unknown
    speed: float | None        # bytes/second
    eta: float | None          # seconds
    downloaded: int | None
    total: int | None
    phase: str                 # "starting", "downloading", "processing", "converting"
    item: int = 1              # 1-based index when downloading several videos
    items: int = 1
    title: str = ""


@dataclass
class DownloadJob:
    urls: list[str]
    quality: Quality | None
    folder: Path
    login: LoginSource = NONE_SOURCE
    password: str | None = None
    referer: str | None = None
    fmt: OutputFormat = ORIGINAL
    clip: tuple[float, float] | None = None    # (start, end) seconds: download only this part
    subtitles: SubtitleChoice | None = None
    name_prefix: str = ""                      # project prefix, e.g. "Nike_Spring_2026-09-25_"


@dataclass
class DownloadResult:
    files: list[Path] = field(default_factory=list)
    failures: list[tuple[str, ErrorInfo]] = field(default_factory=list)
    cancelled: bool = False

    @property
    def error(self) -> ErrorInfo | None:
        if self.cancelled:
            return classify_error(DownloadCancelled("Cancelled"))
        if not self.files and self.failures:
            return self.failures[0][1]
        return None


# --------------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------------

def human_size(n: float | None) -> str:
    if not n:
        return "?"
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("bytes", "KB", "MB") else f"{n:.1f} {unit}"
        n /= 1024
    return "?"


def human_duration(seconds: float | None) -> str:
    if seconds is None:
        return ""
    s = int(round(seconds))
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


class _YdlLogger:
    """Routes yt-dlp's messages into our log and keeps the last lines for the Details box."""

    def __init__(self, redact: Callable[[str], str]):
        self.lines: deque[str] = deque(maxlen=200)
        self._redact = redact

    def _keep(self, level: int, msg: str) -> None:
        msg = self._redact(msg)
        self.lines.append(msg)
        log.log(level, "yt-dlp: %s", msg)

    def debug(self, msg: str) -> None:
        self._keep(logging.DEBUG, msg)

    def info(self, msg: str) -> None:
        self._keep(logging.INFO, msg)

    def warning(self, msg: str) -> None:
        self._keep(logging.WARNING, msg)

    def error(self, msg: str) -> None:
        self._keep(logging.ERROR, msg)

    def saw(self, needle: str) -> bool:
        return any(needle in line for line in self.lines)


def _height_of(fmt: dict) -> int | None:
    """yt-dlp's notion of resolution: the smaller dimension (so portrait 1080x1920 is "1080p")."""
    h, w = fmt.get("height"), fmt.get("width")
    if isinstance(h, (int, float)) and isinstance(w, (int, float)) and h > 0 and w > 0:
        return int(min(h, w))
    if isinstance(h, (int, float)) and h > 0:
        return int(h)
    return None


def _has_video(fmt: dict) -> bool:
    vcodec = fmt.get("vcodec")
    if vcodec == "none":
        return False
    if vcodec:
        return True
    # Unknown codec: treat as video if it has picture dimensions or a video extension.
    return bool(_height_of(fmt)) or fmt.get("ext") in ("mp4", "webm", "mkv", "mov", "flv")


def _formats_of(info: dict) -> list[dict]:
    fmts = info.get("formats")
    if fmts:
        return [f for f in fmts if not f.get("has_drm")]
    if info.get("url"):
        return [info]
    return []


def _estimate_size(selected: dict, duration: float | None) -> int | None:
    parts = selected.get("requested_formats") or [selected]
    total = 0
    for p in parts:
        size = p.get("filesize") or p.get("filesize_approx")
        if not size and p.get("tbr") and duration:
            size = p["tbr"] * 1000 / 8 * duration
        if not size:
            return None
        total += size
    return int(total)


def _select(ydl, info: dict, quality: Quality) -> dict | None:
    """Run yt-dlp's own format selector offline, so sizes match what will be downloaded."""
    opts = format_opts(quality, ORIGINAL)
    ydl.params["format"] = opts["format"]
    ydl.params["format_sort"] = opts["format_sort"]
    slim = {k: v for k, v in info.items() if k not in ("requested_formats", "requested_downloads")}
    try:
        return ydl.process_ie_result(copy.deepcopy(slim), download=False)
    except Exception as e:  # noqa: BLE001 - sizing is best-effort
        log.debug("Format selection for %s failed: %s", quality.key, e)
        return None


def _selector_ydl():
    params = {
        "quiet": True, "no_warnings": True, "simulate": True, "check_formats": False,
        "logger": _YdlLogger(lambda s: s), "allow_unplayable_formats": False,
    }
    # yt-dlp picks different formats when it can't find FFmpeg (no merging), so the
    # estimate must see the same FFmpeg the real download uses.
    ff = paths.ffmpeg_dir()
    if ff:
        params["ffmpeg_location"] = str(ff)
    return yt_dlp.YoutubeDL(params)


def has_video_formats(info: dict) -> bool:
    return any(_has_video(f) for f in _formats_of(info))


def list_qualities(info: dict) -> list[Quality]:
    """Video quality choices built from the formats the source actually has.

    "Best" plus each real height (deduplicated, highest first), with the size
    yt-dlp's own format selector would download. Audio-only sources get no choices (their
    formats are MP3/WAV, see :data:`FORMATS`).
    """
    fmts = _formats_of(info)
    duration = info.get("duration")
    if not any(_has_video(f) for f in fmts):
        return []
    heights = sorted({h for f in fmts if _has_video(f) and (h := _height_of(f))}, reverse=True)

    best_h = heights[0] if heights else None
    out = [Quality("best", "Best" + (f" ({best_h}p)" if best_h else " available"))]
    for h in heights:
        name = HEIGHT_NAMES.get(h)
        out.append(Quality(f"h{h}", f"{h}p ({name})" if name else f"{h}p", height=h))

    sized = []
    with _selector_ydl() as ydl:  # one instance for all choices: creating one is slow
        for q in out:
            selected = _select(ydl, info, q)
            size = _estimate_size(selected, duration) if selected else None
            sized.append(Quality(q.key, q.label, q.height, size))
    return sized


_LANG_NAMES = {
    "en": "English", "es": "Spanish", "fr": "French", "de": "German", "it": "Italian", "pt": "Portuguese",
    "nl": "Dutch", "ja": "Japanese", "ko": "Korean", "zh": "Chinese", "ru": "Russian", "ar": "Arabic",
    "hi": "Hindi", "sv": "Swedish", "da": "Danish", "no": "Norwegian", "fi": "Finnish", "pl": "Polish",
    "tr": "Turkish", "he": "Hebrew", "id": "Indonesian", "th": "Thai", "vi": "Vietnamese", "uk": "Ukrainian",
}


def _lang_label(code: str) -> str:
    base = re.split(r"[-_]", code)[0].lower()
    name = _LANG_NAMES.get(base, code)
    region = code[len(base) + 1:] if len(code) > len(base) else ""
    if region and region.lower() not in ("orig",):
        name = f"{name} ({region})"
    return name


def list_subtitles(info: dict) -> list[SubtitleChoice]:
    """Subtitle languages the video has: uploaded ones first, then the auto-generated
    captions in the video's own language (not the 100+ machine translations)."""
    manual = [k for k in (info.get("subtitles") or {}) if k != "live_chat"]
    choices = [SubtitleChoice(k, _lang_label(k)) for k in manual]
    auto = info.get("automatic_captions") or {}
    originals = [k for k in auto if k.endswith("-orig")]
    if not originals:
        lang = (info.get("language") or "").split("-")[0]
        originals = [k for k in auto if lang and k == lang] or [k for k in auto if k == "en"]
    for k in originals:
        base = k[:-5] if k.endswith("-orig") else k
        if base not in manual:
            choices.append(SubtitleChoice(k, f"{_lang_label(base)} (auto-generated)", auto=True))
    en_first = sorted(choices, key=lambda c: (not c.lang.startswith("en"), c.auto))
    return en_first


# Held while warm_up() runs, so a link check never imports yt-dlp's extractor modules at the
# same time from a second thread; a check that arrives early waits (about a second at most).
_WARM_LOCK = threading.Lock()


def warm_up() -> None:
    """Compile yt-dlp's URL patterns ahead of time.

    The first link check otherwise spends most of a second compiling ~1,800
    regular expressions. Run this in a background thread at startup.
    """
    with _WARM_LOCK:
        _warm_up()


def _warm_up() -> None:
    t0 = time.perf_counter()
    try:
        from yt_dlp.extractor import gen_extractor_classes
        for ie in gen_extractor_classes():
            try:
                ie.suitable("https://example.com/watch?v=warmup")
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0)  # yield the GIL to the UI thread between extractors
    except Exception as e:  # noqa: BLE001
        log.debug("Warm-up failed: %s", e)
    log.debug("Engine warm-up took %.2fs", time.perf_counter() - t0)


def default_quality(qualities: list[Quality], preferred: str) -> Quality | None:
    """Pick the Settings default ("best", "1080", "720", "audio") if available, else Best."""
    if not qualities:
        return None
    wanted = {"best": "best", "audio": "audio"}.get(preferred, f"h{preferred}")
    for q in qualities:
        if q.key == wanted:
            return q
    return qualities[0]


FINAL_EXTS = tuple(sorted({"mp4", "mp3", "mov", "wav", "jpg"}))


def unique_stem(folder: Path, stem: str, ext: str) -> str:
    """``stem`` or ``stem (1)``, ``stem (2)``... so that nothing in ``folder`` is overwritten.

    A name is taken if ``name.<ext>`` exists, or if any other ``name.*`` file exists
    that isn't a finished MP4/MP3 (for example ``name.webm`` or ``name.f137.mp4.part``),
    because yt-dlp's intermediate files could collide with it. So the MP3 of a video
    whose MP4 is already there is still saved as ``name.mp3``.
    """
    try:
        names = {n.lower() for n in os.listdir(folder)}
    except OSError:
        names = set()

    def taken(candidate: str) -> bool:
        prefix = (candidate + ".").lower()
        if f"{candidate}.{ext}".lower() in names:
            return True
        return any(n.startswith(prefix) and n[len(prefix):] not in FINAL_EXTS for n in names)

    if not taken(stem):
        return stem
    n = 1
    while taken(f"{stem} ({n})"):
        n += 1
    return f"{stem} ({n})"


def safe_folder_name(name: str) -> str:
    """A Windows-safe folder name for a playlist title."""
    from yt_dlp.utils import sanitize_filename
    cleaned = sanitize_filename(name or "", restricted=False).strip(" .")
    return cleaned[:100].strip(" .") or "Playlist"


def fetch_thumbnail(url: str, timeout: float = 10) -> bytes | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - https thumbnail URL
            return resp.read(5_000_000)
    except Exception as e:  # noqa: BLE001
        log.debug("Thumbnail download failed: %s", e)
        return None


# --------------------------------------------------------------------------------------------
# Engine
# --------------------------------------------------------------------------------------------

# Values that must never reach a log file (video passwords). Shared with the app's log filter.
_SECRETS: set[str] = set()
_COOKIE_RX = re.compile(r"(?i)((?:set-)?cookie:\s*)[^\n]+")


def register_secret(value: str | None) -> None:
    if value:
        _SECRETS.add(value)


def redact(text: str) -> str:
    """Mask registered secrets and cookie headers in ``text``."""
    for secret in _SECRETS:
        text = text.replace(secret, "********")
    return _COOKIE_RX.sub(r"\1<redacted>", text)


def put_bundled_tools_on_path() -> None:
    """Add the bundled FFmpeg folder to this process's PATH.

    yt-dlp passes ``ffmpeg_location`` to most of its FFmpeg use, but a few checks (for
    example "can this video be downloaded partially?" for clip ranges) only search PATH.
    Without this, clipping fails on PCs without FFmpeg installed system-wide.
    """
    ff = paths.ffmpeg_dir()
    if not ff:
        return
    current = os.environ.get("PATH", "")
    if str(ff) not in current.split(os.pathsep):
        os.environ["PATH"] = str(ff) + os.pathsep + current


def register_extra_browsers() -> None:
    """Teach yt-dlp to read Arc's logins.

    Arc is a Chromium browser that yt-dlp doesn't know by name. Reading it works like Chrome;
    only the folder and (on a Mac) the Keychain entry differ. This hooks into yt-dlp's
    per-browser settings; if a future engine changes them, Arc is simply skipped and the
    other browsers are tried.
    """
    try:
        from yt_dlp import cookies as ydl_cookies
        if "arc" in ydl_cookies.SUPPORTED_BROWSERS:
            return
        original = ydl_cookies._get_chromium_based_browser_settings

        def settings(browser_name):
            if browser_name != "arc":
                return original(browser_name)
            from .browsers import detect_chromium
            arc = next((s for s in detect_chromium() if s.ydl_browser == "arc"), None)
            folder = Path(arc.profile).parent if arc and arc.profile else Path()
            return {"browser_dir": str(folder), "keyring_name": "Arc", "supports_profiles": True}

        ydl_cookies._get_chromium_based_browser_settings = settings
        ydl_cookies.CHROMIUM_BASED_BROWSERS.add("arc")
        ydl_cookies.SUPPORTED_BROWSERS.add("arc")
    except Exception as e:  # noqa: BLE001 - Arc support is optional
        log.warning("Arc logins can't be read with this engine version: %s", e)


# Results that mean "this login didn't work": try the next browser.
LOGIN_RETRY_STATES = frozenset({Status.NEEDS_LOGIN, Status.COOKIES_LOCKED, Status.COOKIES_UNREADABLE,
                                Status.COOKIES_MISSING})


class Engine:
    def __init__(self) -> None:
        self._cancel = threading.Event()
        put_bundled_tools_on_path()
        register_extra_browsers()

    # -- options ---------------------------------------------------------------------------

    @staticmethod
    def redact(text: str) -> str:
        return redact(text)

    def _base_opts(self, login: LoginSource, password: str | None, referer: str | None,
                   logger: _YdlLogger) -> dict:
        register_secret(password)
        opts: dict = {
            "quiet": True,
            "no_warnings": False,
            "noprogress": True,
            "logger": logger,
            "noplaylist": True,
            "windowsfilenames": True,
            "trim_file_name": 150,
            "socket_timeout": 20,
            "retries": 5,
            "fragment_retries": 10,
            # Streams made of many small pieces (HLS/DASH, e.g. Vimeo) download 4 pieces at once.
            "concurrent_fragment_downloads": 4,
            "overwrites": False,
            "continuedl": False,
            "allow_unplayable_formats": False,  # never; see module docstring
            "cachedir": str(paths.app_data_dir() / "cache"),
            "color": {"stdout": "no_color", "stderr": "no_color"},
        }
        ff = paths.ffmpeg_dir()
        if ff:
            opts["ffmpeg_location"] = str(ff)
        deno = paths.deno_exe()
        if deno:
            opts["js_runtimes"] = {"deno": {"path": str(deno)}}
        cookies = login.cookiesfrombrowser()
        if cookies:
            opts["cookiesfrombrowser"] = cookies
        if password:
            opts["videopassword"] = password
        if referer:
            opts["http_headers"] = {"Referer": referer}
        return opts

    def _classify(self, exc: BaseException, login: LoginSource, password: str | None,
                  referer: str | None) -> ErrorInfo:
        info = classify_error(exc, browser=login.app_name, password_given=bool(password),
                              referer_given=bool(referer))
        return ErrorInfo(info.status, info.message, self.redact(info.detail))

    # -- check -----------------------------------------------------------------------------

    def check_link(self, req: CheckRequest,
                   on_attempt: Callable[[LoginSource], None] | None = None) -> CheckResult:
        """Check a link, trying each browser login in turn until one works.

        ``on_attempt`` is called (from this thread) before each retry with the login about to be
        tried, so the UI can say "Trying your login from Chrome…".
        """
        with _WARM_LOCK:
            pass  # wait for a running warm-up to finish
        order: list[LoginSource] = []
        for login in (req.login, *req.fallback_logins):
            if all(login.key != o.key for o in order):
                order.append(login)
        attempts: list[tuple[LoginSource, ErrorInfo]] = []
        res = CheckResult(classify_error("No login worked"))
        for i, login in enumerate(order):
            if i and on_attempt is not None:
                on_attempt(login)
            if i:
                log.info("Trying the next login: %s", "none" if login.is_none else login.app_name)
            res = self._check_once(dataclasses.replace(req, login=login, fallback_logins=()))
            if res.ok or res.result.status not in LOGIN_RETRY_STATES:
                res.login = login if res.ok else NONE_SOURCE
                res.failed_logins = tuple((a.key, info.status) for a, info in attempts)
                return res
            attempts.append((login, res.result))
            log.info("Login %s didn't work: %s", "none" if login.is_none else login.app_name,
                     res.result.status.value)
        if len(attempts) > 1:
            res.result = login_attempts_failed([(a.app_name if not a.is_none else None, info)
                                                for a, info in attempts])
        res.failed_logins = tuple((a.key, info.status) for a, info in attempts)
        return res

    def _check_once(self, req: CheckRequest) -> CheckResult:
        url = normalize_url(req.url)
        if not url:
            return CheckResult(classify_error(f"{req.url!r} is not a valid URL"))
        logger = _YdlLogger(self.redact)
        opts = self._base_opts(req.login, req.password, req.referer, logger)
        log.info("Checking link (login=%s, password=%s, referer=%s)",
                 req.login.key if not req.login.is_none else "none",
                 "yes" if req.password else "no", "yes" if req.referer else "no")
        try:
            with yt_dlp.YoutubeDL({**opts, "extract_flat": "in_playlist"}) as ydl:
                info = ydl.extract_info(url, download=False)
            if info is None:
                raise yt_dlp.utils.ExtractorError("No video could be found", expected=True)

            playlist: PlaylistInfo | None = None
            if info.get("_type") == "playlist":
                playlist = self._playlist_from(info, pure=True)
                if not playlist.entry_urls:
                    return CheckResult(classify_error("No video could be found in this playlist"))
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(playlist.entry_urls[0], download=False)
                url = playlist.entry_urls[0]
            elif _looks_like_playlist_url(url) or logger.saw("--no-playlist"):
                playlist = self._probe_playlist(url, opts)

            if info.get("_has_drm") or (info.get("formats") and all(f.get("has_drm") for f in info["formats"])):
                return CheckResult(classify_error("This video is DRM protected"))

            qualities = list_qualities(info)
            video = VideoInfo(
                title=info.get("title") or info.get("id") or "Video",
                uploader=info.get("uploader") or info.get("channel") or info.get("uploader_id"),
                duration=info.get("duration"),
                thumbnail=info.get("thumbnail"),
                webpage_url=info.get("webpage_url") or url,
                site=info.get("extractor_key"),
            )
            return CheckResult(ready(), url=info.get("webpage_url") or url, video=video,
                               qualities=qualities, playlist=playlist, subtitles=list_subtitles(info),
                               has_video=bool(qualities))
        except Exception as e:  # noqa: BLE001 - every failure becomes a state
            result = self._classify(e, req.login, req.password, req.referer)
            log.info("Check failed: %s", result.status.value)
            log.debug("Check failure detail: %s", result.detail)
            return CheckResult(result)

    @staticmethod
    def _playlist_from(info: dict, pure: bool) -> PlaylistInfo:
        entries = [e for e in (info.get("entries") or []) if e]
        urls = [e.get("url") or e.get("webpage_url") for e in entries]
        urls = [u for u in urls if u]
        count = info.get("playlist_count") or len(urls)
        return PlaylistInfo(title=info.get("title") or "Playlist", count=int(count),
                            entry_urls=urls, pure=pure)

    def _probe_playlist(self, url: str, opts: dict) -> PlaylistInfo | None:
        """For a "video inside a playlist" link, find out how many videos the playlist has."""
        try:
            with yt_dlp.YoutubeDL({**opts, "noplaylist": False, "extract_flat": True}) as ydl:
                pl = ydl.extract_info(url, download=False)
        except Exception as e:  # noqa: BLE001 - playlist info is optional
            log.info("Playlist probe failed: %s", e)
            return None
        if not pl or pl.get("_type") != "playlist":
            return None
        playlist = self._playlist_from(pl, pure=False)
        return playlist if len(playlist.entry_urls) > 1 else None

    # -- download --------------------------------------------------------------------------

    def cancel(self) -> None:
        self._cancel.set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def download(self, job: DownloadJob, on_progress: Callable[[Progress], None]) -> DownloadResult:
        self._cancel.clear()
        result = DownloadResult()
        folder = Path(job.folder)
        folder.mkdir(parents=True, exist_ok=True)
        total = len(job.urls)
        for index, url in enumerate(job.urls, start=1):
            if self._cancel.is_set():
                result.cancelled = True
                break
            try:
                path = self._download_one(url, job, folder, index, total, on_progress)
                result.files.append(path)
            except _Cancelled:
                result.cancelled = True
                break
            except _Failed as f:
                result.failures.append((f.title, f.info))
                if f.info.status is Status.CANCELLED:
                    result.cancelled = True
                    break
                if f.info.status in (Status.SAVE_FAILED, Status.APP_PROBLEM, Status.COOKIES_LOCKED,
                                     Status.COOKIES_UNREADABLE):
                    break  # the next videos would fail the same way
        return result

    def _download_one(self, url: str, job: DownloadJob, folder: Path, index: int, total: int,
                      on_progress: Callable[[Progress], None]) -> Path:
        logger = _YdlLogger(self.redact)
        fmt = job.fmt
        opts = self._base_opts(job.login, job.password, job.referer, logger)
        opts.update(format_opts(job.quality, fmt))
        if job.clip:
            from yt_dlp.utils import download_range_func
            opts["download_ranges"] = download_range_func(None, [job.clip])
            opts["force_keyframes_at_cuts"] = fmt.kind == "video"  # exact cut points
        if job.subtitles and fmt.kind == "video":
            sub = job.subtitles
            opts.update({
                "writesubtitles": not sub.auto,
                "writeautomaticsub": sub.auto,
                "subtitleslangs": [sub.lang],
                "subtitlesformat": "srt/vtt/best",
            })
            opts["postprocessors"] = [*opts.get("postprocessors", []),
                                      {"key": "FFmpegSubtitlesConvertor", "format": "srt", "when": "before_dl"}]
        state = _ProgressState(index=index, items=total)

        def progress_hook(d: dict) -> None:
            if self._cancel.is_set():
                raise DownloadCancelled("Cancelled by user")
            p = state.update(d)
            if p:
                on_progress(p)

        def pp_hook(d: dict) -> None:
            if self._cancel.is_set():
                raise DownloadCancelled("Cancelled by user")
            if d.get("status") == "started":
                on_progress(state.processing())

        opts["progress_hooks"] = [progress_hook]
        opts["postprocessor_hooks"] = [pp_hook]
        opts["paths"] = {"home": str(folder)}
        on_progress(Progress(None, None, None, None, None, "starting", index, total))

        before = _listdir(folder)
        stem = ""
        title = url
        outcome: ErrorInfo | None = None
        cancelled = False
        final: Path | None = None
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                # Resolve the video and pick formats first, so we know the title and can
                # choose a file name that doesn't overwrite anything.
                info = ydl.extract_info(url, download=False)
                if info is None:
                    raise yt_dlp.utils.ExtractorError("No video could be found", expected=True)
                if self._cancel.is_set():
                    raise DownloadCancelled("Cancelled by user")
                title = info.get("title") or title
                state.title = title
                state.parts = info.get("requested_formats") or []
                state.duration = info.get("duration")
                base = Path(ydl.prepare_filename(info, outtmpl="%(title)s")).name or "video"
                stem = unique_stem(folder, job.name_prefix + base + clip_suffix(job.clip), fmt.ext)
                # Files that get converted afterwards are downloaded under a temporary name.
                download_stem = stem + ".source" if fmt.convert else stem
                ydl.params["outtmpl"]["default"] = download_stem.replace("%", "%%") + ".%(ext)s"
                log.info("Downloading item %d/%d as %s.%s", index, total, stem, fmt.ext)
                ydl.process_ie_result(info, download=True)
            if fmt.convert:
                source = _find_output(folder, download_stem, before)
                final = folder / f"{stem}.{fmt.ext}"
                duration = (job.clip[1] - job.clip[0]) if job.clip else info.get("duration")
                self._convert(source, final, fmt.convert, _fps_of(info), duration,
                              lambda pct: on_progress(Progress(pct, None, None, None, None, "converting",
                                                               index, total, title)))
                source.unlink(missing_ok=True)
                for sub in folder.glob(f"{_glob_escape(download_stem)}.*.srt"):
                    sub.replace(folder / (stem + sub.name[len(download_stem):]))
        except BaseException as e:  # noqa: BLE001
            cancelled = isinstance(e, (DownloadCancelled, KeyboardInterrupt)) or self._cancel.is_set()
            if not cancelled:
                outcome = self._classify(e, job.login, job.password, job.referer)
            # Leave the except block before cleaning up: the traceback keeps yt-dlp's frames,
            # and with them its open .part file, alive. Windows can't delete open files.
        if cancelled or outcome is not None:
            gc.collect()
            self._cleanup(folder, stem, before)
            if cancelled:
                log.info("Download cancelled; partial files removed")
                raise _Cancelled()
            log.info("Download failed: %s", outcome.status.value)
            log.debug("Download failure detail: %s", outcome.detail)
            raise _Failed(title, outcome)

        final = final or folder / f"{stem}.{fmt.ext}"
        if final.is_file():
            return final
        found = _find_output(folder, stem, before, exclude_ext=(".srt",))
        if found:
            return found
        raise _Failed(title, classify_error("Download finished but the file was not found"))

    def _convert(self, src: Path, dst: Path, kind: str, fps: float | None, duration: float | None,
                 on_percent: Callable[[float | None], None]) -> None:
        """Re-encode ``src`` into an editing-friendly file. Cancellable; reports percent."""
        ffmpeg = _ffmpeg_exe()
        rate = _frame_rate_arg(fps)
        if kind == "edit":
            video = ["-c:v", "libx264", "-preset", "medium", "-crf", "17", "-pix_fmt", "yuv420p",
                     "-fps_mode", "cfr", *(["-r", rate] if rate else []), "-movflags", "+faststart"]
            audio = ["-c:a", "aac", "-b:a", "320k", "-ar", "48000"]
        elif kind == "prores":
            video = ["-c:v", "prores_ks", "-profile:v", "3", "-vendor", "apl0", "-pix_fmt", "yuv422p10le",
                     "-fps_mode", "cfr", *(["-r", rate] if rate else [])]
            audio = ["-c:a", "pcm_s16le", "-ar", "48000"]
        else:
            raise ValueError(kind)
        tmp = dst.with_name(dst.stem + ".converting" + dst.suffix)
        cmd = [ffmpeg, "-hide_banner", "-nostdin", "-y", "-i", str(src), "-map", "0:v:0", "-map", "0:a?",
               *video, *audio, "-progress", "pipe:1", "-nostats", "-loglevel", "error", str(tmp)]
        log.info("Converting to %s (%s)", kind, dst.suffix)
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        err_lines: deque[str] = deque(maxlen=20)
        threading.Thread(target=lambda: err_lines.extend(proc.stderr), daemon=True).start()
        on_percent(0.0 if duration else None)
        try:
            for line in proc.stdout:
                if self._cancel.is_set():
                    proc.kill()
                    proc.wait()
                    tmp.unlink(missing_ok=True)
                    raise DownloadCancelled("Cancelled by user")
                key, _, value = line.strip().partition("=")
                if key == "out_time_us" and duration and value.isdigit():
                    on_percent(min(100.0, int(value) / 1e6 / duration * 100))
            code = proc.wait()
        finally:
            if proc.poll() is None:
                proc.kill()
        if code != 0:
            tmp.unlink(missing_ok=True)
            raise RuntimeError("FFmpeg conversion failed: " + " ".join(err_lines)[-500:])
        tmp.replace(dst)
        on_percent(100.0)

    @staticmethod
    def _cleanup(folder: Path, stem: str, before: set[str]) -> None:
        """Delete every new file this download created (partials, fragments, unmerged parts)."""
        if not stem:
            return
        prefix = stem + "."
        for name in _listdir(folder) - before:
            if not name.startswith(prefix):
                continue
            target = folder / name
            for attempt in range(15):
                try:
                    if target.is_dir():
                        break
                    target.unlink()
                    log.info("Removed partial file %s", name)
                    break
                except FileNotFoundError:
                    break
                except OSError:
                    # Windows may briefly hold the file (antivirus, ffmpeg exiting).
                    time.sleep(0.2 * (attempt + 1))
            else:
                log.warning("Could not remove partial file %s", name)


def _find_output(folder: Path, stem: str, before: set[str], exclude_ext: tuple[str, ...] = ()) -> Path | None:
    """The largest new finished file named ``stem.<ext>`` (ignores partials and subtitles)."""
    prefix = stem + "."
    found = [folder / n for n in _listdir(folder) - before
             if n.startswith(prefix) and "." not in n[len(prefix):]
             and not n.endswith(PARTIAL_SUFFIXES) and not n.endswith(exclude_ext or ("\0",))]
    return max(found, key=lambda p: p.stat().st_size) if found else None


def _glob_escape(text: str) -> str:
    return re.sub(r"([\[\]*?])", r"[\1]", text)


def _fps_of(info: dict) -> float | None:
    for f in info.get("requested_formats") or [info]:
        if f.get("vcodec") not in (None, "none") and f.get("fps"):
            return float(f["fps"])
    return info.get("fps")


def _frame_rate_arg(fps: float | None) -> str | None:
    """FFmpeg -r value; NTSC rates become exact fractions (29.97 -> 30000/1001)."""
    if not fps or fps <= 0:
        return None
    for ntsc, frac in ((23.976, "24000/1001"), (29.97, "30000/1001"), (59.94, "60000/1001")):
        if abs(fps - ntsc) < 0.01:
            return frac
    return str(round(fps, 3)).rstrip("0").rstrip(".")


def _ffmpeg_exe() -> str:
    ff = paths.ffmpeg_dir()
    if ff:
        return str(ff / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg"))
    import shutil
    found = shutil.which("ffmpeg")
    if not found:
        raise RuntimeError("ffmpeg not found")
    return found


class _Cancelled(Exception):
    pass


class _Failed(Exception):
    def __init__(self, title: str, info: ErrorInfo):
        super().__init__(info.message)
        self.title = title
        self.info = info


def _listdir(folder: Path) -> set[str]:
    try:
        return set(os.listdir(folder))
    except OSError:
        return set()


@dataclass
class _ProgressState:
    """Combines the separate video and audio downloads of one item into a single percentage."""

    index: int = 1
    items: int = 1
    title: str = ""
    done_bytes: dict[str, int] = field(default_factory=dict)  # finished parts: filename -> bytes
    current: str | None = None
    last_percent: float = 0.0
    parts: list[dict] = field(default_factory=list)
    duration: float | None = None

    def update(self, d: dict) -> Progress | None:
        status = d.get("status")
        filename = d.get("filename") or ""
        info = d.get("info_dict") or {}
        # yt-dlp only passes the current part's info to hooks, so the full list of parts
        # (video + audio) is taken from the selection made before the download started.
        parts = self.parts or info.get("requested_formats") or []
        duration = self.duration or info.get("duration")
        weights = self._weights(parts, duration)
        part_idx = self._part_index(parts, info.get("format_id"))

        if status == "finished":
            total = d.get("total_bytes") or d.get("downloaded_bytes") or 0
            self.done_bytes[filename] = int(total)
            return None
        if status != "downloading":
            return None
        self.current = filename
        downloaded = d.get("downloaded_bytes") or 0
        total = d.get("total_bytes") or d.get("total_bytes_estimate")
        frac = None
        if d.get("fragment_count") and d.get("fragment_index") is not None:
            # Fragment counts are exact; byte totals are only estimates while fragments
            # arrive, so keep the byte estimate within the current fragment's bounds.
            count, idx = d["fragment_count"], d["fragment_index"]
            low, high = min(1.0, idx / count), min(1.0, (idx + 1) / count)
            frac = min(max(downloaded / total, low), high) if total else low
        elif total:
            frac = min(1.0, downloaded / total)

        percent = None
        if frac is not None:
            if weights and part_idx is not None:
                before = sum(weights[:part_idx])
                percent = 100 * (before + weights[part_idx] * frac)
            else:
                percent = 100 * frac
            # Never move the bar backwards within one video.
            percent = max(percent, self.last_percent)
            self.last_percent = percent
        speed = d.get("speed")
        eta = d.get("eta")
        if weights and part_idx is not None and speed and total:
            # Remaining bytes for this part plus the parts still to come.
            later = sum(self._sizes(parts, duration)[part_idx + 1:] or [0])
            eta = (max(0, total - downloaded) + later) / speed
        return Progress(percent, speed, eta, downloaded, total, "downloading",
                        self.index, self.items, self.title)

    def processing(self) -> Progress:
        return Progress(None, None, None, None, None, "processing", self.index, self.items, self.title)

    @staticmethod
    def _sizes(parts: list[dict], duration: float | None) -> list[float]:
        sizes = []
        for p in parts:
            s = p.get("filesize") or p.get("filesize_approx")
            if not s and p.get("tbr") and duration:
                s = p["tbr"] * 1000 / 8 * duration
            sizes.append(float(s or 0))
        return sizes

    def _weights(self, parts: list[dict], duration: float | None) -> list[float]:
        if len(parts) < 2:
            return []
        sizes = self._sizes(parts, duration)
        if all(sizes):
            total = sum(sizes)
            return [s / total for s in sizes]
        return [1 / len(parts)] * len(parts)

    @staticmethod
    def _part_index(parts: list[dict], format_id: str | None) -> int | None:
        for i, p in enumerate(parts):
            if p.get("format_id") == format_id:
                return i
        return None
