"""Thin wrapper around ``yt_dlp.YoutubeDL``: check a link, list qualities, download, cancel.

All functions here are blocking and are meant to be called from a worker
thread. They never touch the UI; progress is reported through a callback.

DRM: this module never enables ``allow_unplayable_formats`` and stops with a
"copy-protected" state whenever yt-dlp reports DRM. There is no workaround.
"""

from __future__ import annotations

import copy
import logging
import os
import re
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
from .errors import ErrorInfo, Status, classify_error, ready

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
    key: str                 # "best", "h1080", "audio"
    label: str               # "1080p (Full HD)"
    height: int | None = None
    audio_only: bool = False
    size: int | None = None  # approximate bytes, if known

    @property
    def ext(self) -> str:
        return "mp3" if self.audio_only else "mp4"

    @property
    def display(self) -> str:
        return f"{self.label}  ·  ~{human_size(self.size)}" if self.size else self.label

    def ydl_opts(self) -> dict:
        """Format selection for this choice. Prefers H.264 video + AAC/M4A audio in MP4."""
        if self.audio_only:
            return {
                "format": "ba/b",
                "format_sort": ["acodec:aac", "abr"],
                "postprocessors": [{
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": str(MP3_KBPS),
                }],
            }
        res = f"res:{self.height}" if self.height else "res"
        return {
            "format": "bv*+ba/b",
            "format_sort": [res, "vcodec:h264", "acodec:aac", "ext:mp4:m4a"],
            "merge_output_format": "mp4",
            # A single pre-merged file (e.g. WebM) is remuxed so the result is always .mp4.
            "postprocessors": [{"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"}],
        }


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


@dataclass
class CheckResult:
    result: ErrorInfo
    url: str | None = None                 # normalized URL of the single video
    video: VideoInfo | None = None
    qualities: list[Quality] = field(default_factory=list)
    playlist: PlaylistInfo | None = None

    @property
    def ok(self) -> bool:
        return self.result.status is Status.READY


@dataclass
class Progress:
    percent: float | None      # 0..100 for the current video, None if unknown
    speed: float | None        # bytes/second
    eta: float | None          # seconds
    downloaded: int | None
    total: int | None
    phase: str                 # "starting", "downloading", "processing"
    item: int = 1              # 1-based index when downloading several videos
    items: int = 1
    title: str = ""


@dataclass
class DownloadJob:
    urls: list[str]
    quality: Quality
    folder: Path
    login: LoginSource = NONE_SOURCE
    password: str | None = None
    referer: str | None = None


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


def _select(info: dict, quality: Quality) -> dict | None:
    """Run yt-dlp's own format selector offline, so sizes match what will be downloaded."""
    params = {
        "quiet": True, "no_warnings": True, "simulate": True, "check_formats": False,
        "logger": _YdlLogger(lambda s: s), "allow_unplayable_formats": False,
        **{k: v for k, v in quality.ydl_opts().items() if k != "postprocessors"},
    }
    slim = {k: v for k, v in info.items() if k not in ("requested_formats", "requested_downloads")}
    try:
        with yt_dlp.YoutubeDL(params) as ydl:
            return ydl.process_ie_result(copy.deepcopy(slim), download=False)
    except Exception as e:  # noqa: BLE001 - sizing is best-effort
        log.debug("Format selection for %s failed: %s", quality.key, e)
        return None


def list_qualities(info: dict) -> list[Quality]:
    """Quality choices built from the formats the source actually has.

    Always: "Best available (MP4)", each real height (deduplicated, highest first),
    and "Audio only (MP3)". Sources with no video only get the audio choice.
    """
    fmts = _formats_of(info)
    duration = info.get("duration")
    has_video = any(_has_video(f) for f in fmts)
    heights = sorted({h for f in fmts if _has_video(f) and (h := _height_of(f))}, reverse=True)

    out: list[Quality] = []
    if has_video:
        best_h = heights[0] if heights else None
        label = "Best available (MP4)" + (f" — {best_h}p" if best_h else "")
        out.append(Quality("best", label))
        for h in heights:
            name = HEIGHT_NAMES.get(h)
            out.append(Quality(f"h{h}", f"{h}p ({name})" if name else f"{h}p", height=h))
    out.append(Quality("audio", "Audio only (MP3)", audio_only=True))

    sized = []
    for q in out:
        size = None
        if q.audio_only and duration:
            size = int(duration * MP3_KBPS * 1000 / 8)
        else:
            selected = _select(info, q)
            if selected:
                size = _estimate_size(selected, duration)
        sized.append(Quality(q.key, q.label, q.height, q.audio_only, size))
    return sized


def default_quality(qualities: list[Quality], preferred: str) -> Quality | None:
    """Pick the Settings default ("best", "1080", "720", "audio") if available, else Best."""
    if not qualities:
        return None
    wanted = {"best": "best", "audio": "audio"}.get(preferred, f"h{preferred}")
    for q in qualities:
        if q.key == wanted:
            return q
    return qualities[0]


def unique_stem(folder: Path, stem: str, ext: str) -> str:
    """``stem`` or ``stem (1)``, ``stem (2)``... so that nothing in ``folder`` is overwritten.

    A candidate is taken if the final file exists or any file starts with ``candidate.``
    (for example a leftover ``.part`` from another program).
    """
    try:
        names = {n.lower() for n in os.listdir(folder)}
    except OSError:
        names = set()

    def taken(candidate: str) -> bool:
        prefix = (candidate + ".").lower()
        return f"{candidate}.{ext}".lower() in names or any(n.startswith(prefix) for n in names)

    if not taken(stem):
        return stem
    n = 1
    while taken(f"{stem} ({n})"):
        n += 1
    return f"{stem} ({n})"


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

class Engine:
    def __init__(self) -> None:
        self._cancel = threading.Event()
        self._secrets: set[str] = set()

    # -- options ---------------------------------------------------------------------------

    def redact(self, text: str) -> str:
        for secret in self._secrets:
            if secret:
                text = text.replace(secret, "********")
        return re.sub(r"(?i)(cookie:\s*)[^\n]+", r"\1<redacted>", text)

    def _base_opts(self, login: LoginSource, password: str | None, referer: str | None,
                   logger: _YdlLogger) -> dict:
        self._secrets = {password} if password else set()
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

    def check_link(self, req: CheckRequest) -> CheckResult:
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
                               qualities=qualities, playlist=playlist)
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
        opts = self._base_opts(job.login, job.password, job.referer, logger)
        opts.update(job.quality.ydl_opts())
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
                stem = unique_stem(folder, base, job.quality.ext)
                ydl.params["outtmpl"]["default"] = stem.replace("%", "%%") + ".%(ext)s"
                log.info("Downloading item %d/%d as %s.%s", index, total, stem, job.quality.ext)
                ydl.process_ie_result(info, download=True)
        except BaseException as e:
            self._cleanup(folder, stem, before)
            if isinstance(e, (DownloadCancelled, KeyboardInterrupt)) or self._cancel.is_set():
                log.info("Download cancelled; partial files removed")
                raise _Cancelled() from e
            info_err = self._classify(e, job.login, job.password, job.referer)
            log.info("Download failed: %s", info_err.status.value)
            log.debug("Download failure detail: %s", info_err.detail)
            raise _Failed(title, info_err) from e

        final = folder / f"{stem}.{job.quality.ext}"
        if final.is_file():
            return final
        new = [folder / n for n in _listdir(folder) - before
               if n.startswith(stem + ".") and not n.endswith(PARTIAL_SUFFIXES)]
        if new:
            return max(new, key=lambda p: p.stat().st_size)
        raise _Failed(title, classify_error("Download finished but the file was not found"))

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
            for attempt in range(10):
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
