"""Find browser profiles whose saved login can be handed to yt-dlp (private/sign-in videos).

Firefox-family browsers (Firefox, Zen, LibreWolf, Floorp) all store cookies in
Firefox's ``cookies.sqlite`` format, so any of their profiles can be passed to
yt-dlp as ``cookiesfrombrowser=('firefox', <profile path>, None, None)``.

Chrome, Edge and Brave are listed too, but Windows encryption usually blocks
reading them; the UI shows a note and a plain message if the read fails.

Nothing here reads cookie values. We only check that ``cookies.sqlite`` exists.
"""

from __future__ import annotations

import configparser
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

COOKIE_DB = "cookies.sqlite"

# (display name, folder under %APPDATA% that holds "Profiles" and profiles.ini)
FIREFOX_FAMILY_WINDOWS = (
    ("Firefox", Path("Mozilla") / "Firefox"),
    ("Zen", Path("zen")),
    ("LibreWolf", Path("librewolf")),
    ("Floorp", Path("Floorp")),
)

# Non-Windows locations, only so the code stays portable (we build and test on Windows).
FIREFOX_FAMILY_OTHER = (
    ("Firefox", (Path(".mozilla/firefox"), Path("Library/Application Support/Firefox"))),
    ("Zen", (Path(".zen"), Path("Library/Application Support/zen"))),
    ("LibreWolf", (Path(".librewolf"), Path("Library/Application Support/librewolf"))),
    ("Floorp", (Path(".floorp"), Path("Library/Application Support/Floorp"))),
)

# (yt-dlp browser name, display name, user-data folder under %LOCALAPPDATA%)
CHROMIUM_WINDOWS = (
    ("chrome", "Chrome", Path("Google") / "Chrome" / "User Data"),
    ("edge", "Edge", Path("Microsoft") / "Edge" / "User Data"),
    ("brave", "Brave", Path("BraveSoftware") / "Brave-Browser" / "User Data"),
)

CHROMIUM_NOTE = (
    "Windows encryption usually blocks reading logins from Chrome, Edge and Brave. "
    "For private videos, use Firefox, Zen, LibreWolf or Floorp instead."
)

LOGIN_TOOLTIP = (
    "Only needed for private videos. Uses the login already saved in your browser; "
    "your password is never seen or stored."
)

NONE_KEY = "none"
CUSTOM_LABEL = "Custom profile folder…"


@dataclass(frozen=True)
class LoginSource:
    key: str                 # stable id saved in settings
    label: str               # what the dropdown shows
    app_name: str | None     # "Zen", "Chrome"... used in messages ("Close Zen completely")
    ydl_browser: str | None  # "firefox", "chrome", "edge", "brave" or None
    profile: str | None      # profile folder for Firefox-family sources
    mtime: float = 0.0
    note: str = ""

    @property
    def is_none(self) -> bool:
        return self.ydl_browser is None

    @property
    def is_chromium(self) -> bool:
        return self.ydl_browser in ("chrome", "edge", "brave")

    def cookiesfrombrowser(self) -> tuple | None:
        """The value for yt-dlp's ``cookiesfrombrowser`` option."""
        if self.ydl_browser is None:
            return None
        return (self.ydl_browser, self.profile, None, None)


NONE_SOURCE = LoginSource(NONE_KEY, "None", None, None, None)


def _profile_names(root: Path) -> dict[str, str]:
    """Map resolved profile folder -> Name from ``profiles.ini`` (if present)."""
    names: dict[str, str] = {}
    ini = root / "profiles.ini"
    if not ini.is_file():
        return names
    parser = configparser.RawConfigParser(strict=False)
    try:
        parser.read(ini, encoding="utf-8")
    except (configparser.Error, OSError, UnicodeDecodeError) as e:
        log.debug("Could not parse %s: %s", ini, e)
        return names
    for section in parser.sections():
        if not section.lower().startswith("profile"):
            continue
        rel = parser.get(section, "Path", fallback=None)
        name = parser.get(section, "Name", fallback=None)
        if not rel or not name:
            continue
        is_relative = parser.get(section, "IsRelative", fallback="1").strip() == "1"
        p = (root / rel) if is_relative else Path(rel)
        names[_norm(p)] = name
    return names


def _norm(p: Path) -> str:
    try:
        p = p.resolve()
    except OSError:
        pass
    return os.path.normcase(str(p))


def _folder_label(folder: Path) -> str:
    # Firefox profile folders look like "abcd1234.default-release".
    name = folder.name
    _, dot, rest = name.partition(".")
    return rest if dot and rest else name


def _scan_family(app_name: str, root: Path) -> list[LoginSource]:
    profiles_dir = root / "Profiles"
    if not profiles_dir.is_dir():
        # Linux Firefox keeps profiles directly in the root.
        profiles_dir = root
    if not profiles_dir.is_dir():
        return []
    names = _profile_names(root)
    found: list[LoginSource] = []
    try:
        children = list(profiles_dir.iterdir())
    except OSError:
        return []
    for child in children:
        db = child / COOKIE_DB
        try:
            if not (child.is_dir() and db.is_file()):
                continue
            mtime = max(db.stat().st_mtime, child.stat().st_mtime)
        except OSError:
            continue
        pretty = names.get(_norm(child)) or _folder_label(child)
        found.append(LoginSource(
            key=f"firefox:{child}",
            label=f"{app_name} — {pretty}",
            app_name=app_name,
            ydl_browser="firefox",
            profile=str(child),
            mtime=mtime,
        ))
    return found


def firefox_family_roots(appdata: Path | None = None, home: Path | None = None) -> list[tuple[str, Path]]:
    if sys.platform == "win32" or appdata is not None:
        base = appdata or Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
        return [(name, base / sub) for name, sub in FIREFOX_FAMILY_WINDOWS]
    home = home or Path.home()
    return [(name, home / sub) for name, subs in FIREFOX_FAMILY_OTHER for sub in subs]


def detect_firefox_profiles(appdata: Path | None = None, home: Path | None = None) -> list[LoginSource]:
    """All Firefox-family profiles that have a cookie database, most recently used first."""
    sources: list[LoginSource] = []
    for app_name, root in firefox_family_roots(appdata, home):
        sources.extend(_scan_family(app_name, root))
    sources.sort(key=lambda s: s.mtime, reverse=True)
    return _dedupe_labels(sources)


def _dedupe_labels(sources: list[LoginSource]) -> list[LoginSource]:
    counts: dict[str, int] = {}
    for s in sources:
        counts[s.label] = counts.get(s.label, 0) + 1
    out = []
    for s in sources:
        if counts[s.label] > 1 and s.profile:
            s = LoginSource(s.key, f"{s.label} ({Path(s.profile).name})", s.app_name, s.ydl_browser,
                            s.profile, s.mtime, s.note)
        out.append(s)
    return out


def detect_chromium(localappdata: Path | None = None) -> list[LoginSource]:
    """Chrome/Edge/Brave, if installed. Listed with a note; reading them usually fails on Windows."""
    if sys.platform == "win32" or localappdata is not None:
        base = localappdata or Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        candidates = [(key, name, base / sub) for key, name, sub in CHROMIUM_WINDOWS]
    else:
        home = Path.home()
        candidates = [
            ("chrome", "Chrome", home / ".config" / "google-chrome"),
            ("edge", "Edge", home / ".config" / "microsoft-edge"),
            ("brave", "Brave", home / ".config" / "BraveSoftware" / "Brave-Browser"),
        ]
    return [
        LoginSource(key=key, label=f"{name} (may not work)", app_name=name, ydl_browser=key,
                    profile=None, note=CHROMIUM_NOTE)
        for key, name, folder in candidates if folder.is_dir()
    ]


def custom_source(folder: str | os.PathLike) -> LoginSource | None:
    """A user-chosen Firefox-format profile folder, or None if it has no cookie database."""
    p = Path(folder)
    if not (p / COOKIE_DB).is_file():
        return None
    return LoginSource(key=f"custom:{p}", label=f"Custom — {p.name}", app_name="your browser",
                       ydl_browser="firefox", profile=str(p))


def all_sources(appdata: Path | None = None, localappdata: Path | None = None,
                home: Path | None = None) -> list[LoginSource]:
    """None, then detected Firefox-family profiles, then Chrome/Edge/Brave."""
    return [NONE_SOURCE, *detect_firefox_profiles(appdata, home), *detect_chromium(localappdata)]


AUTO_KEY = "auto"


def automatic(sources: list[LoginSource]) -> LoginSource:
    """The browser login used by default: the most recently used Firefox-family profile.

    Chrome/Edge/Brave are never picked automatically (Windows usually blocks reading them).
    """
    for s in sources:
        if s.ydl_browser == "firefox":
            return s
    return NONE_SOURCE


def resolve(key: str | None, sources: list[LoginSource]) -> LoginSource:
    """Like :func:`from_key`, but understands the "auto" setting."""
    if not key or key == AUTO_KEY:
        return automatic(sources)
    return from_key(key, sources)


def from_key(key: str | None, sources: list[LoginSource]) -> LoginSource:
    """Find the saved login source; fall back to None if it no longer exists."""
    if not key or key == NONE_KEY:
        return NONE_SOURCE
    for s in sources:
        if s.key == key:
            return s
    if key.startswith("custom:"):
        src = custom_source(key[len("custom:"):])
        if src:
            return src
    if key.startswith("firefox:"):
        src = custom_source(key[len("firefox:"):])
        if src:
            # Still on disk but not under a known root any more; keep using it.
            return LoginSource(key, src.label, src.app_name, "firefox", src.profile)
    log.info("Saved login source is no longer available; using None")
    return NONE_SOURCE
