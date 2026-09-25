"""Find browsers whose saved login can be handed to yt-dlp (private/sign-in videos).

Firefox-family browsers (Firefox, Zen, LibreWolf, Floorp) all store cookies in
Firefox's ``cookies.sqlite`` format, so any of their profiles can be passed to
yt-dlp as ``cookiesfrombrowser=('firefox', <profile path>, None, None)``.

Chromium-family browsers (Chrome, Edge, Brave, Arc, Vivaldi, Opera) and Safari
are found too. On a Mac their logins can be read (macOS asks once for
permission). On Windows, Chrome-based browsers usually block it; trying them
fails quickly and the app moves on to the next browser.

When a site needs a login, the app tries these one after another, most
recently used first (see :func:`candidates`).

Nothing here reads cookie values. We only look at whether the cookie files
exist and when they last changed.
"""

from __future__ import annotations

import configparser
import logging
import os
import sys
import time
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

# Chromium-family browsers: (yt-dlp browser name, display name, user-data folder).
# "arc" isn't built into yt-dlp; engine.register_extra_browsers() adds it.
CHROMIUM_WINDOWS = (  # under %LOCALAPPDATA% (Opera: %APPDATA%)
    ("chrome", "Chrome", Path("Google") / "Chrome" / "User Data"),
    ("edge", "Edge", Path("Microsoft") / "Edge" / "User Data"),
    ("brave", "Brave", Path("BraveSoftware") / "Brave-Browser" / "User Data"),
    ("vivaldi", "Vivaldi", Path("Vivaldi") / "User Data"),
)
CHROMIUM_MAC = (  # under ~/Library/Application Support
    ("chrome", "Chrome", Path("Google") / "Chrome"),
    ("arc", "Arc", Path("Arc") / "User Data"),
    ("edge", "Edge", Path("Microsoft Edge")),
    ("brave", "Brave", Path("BraveSoftware") / "Brave-Browser"),
    ("vivaldi", "Vivaldi", Path("Vivaldi")),
    ("opera", "Opera", Path("com.operasoftware.Opera")),
)
CHROMIUM_LINUX = (  # under ~/.config
    ("chrome", "Chrome", Path("google-chrome")),
    ("chromium", "Chromium", Path("chromium")),
    ("edge", "Edge", Path("microsoft-edge")),
    ("brave", "Brave", Path("BraveSoftware") / "Brave-Browser"),
    ("vivaldi", "Vivaldi", Path("vivaldi")),
    ("opera", "Opera", Path("opera")),
)
CHROMIUM_BROWSERS = frozenset({"chrome", "chromium", "edge", "brave", "vivaldi", "opera", "arc"})
# Opera keeps one profile in its user-data folder; yt-dlp finds it without a profile path.
_NO_PROFILES = frozenset({"opera"})

CHROMIUM_NOTE = (
    "Windows encryption usually blocks reading logins from Chrome-based browsers (Chrome, Edge, Brave, Arc). "
    "For private videos, use Firefox, Zen, LibreWolf or Floorp instead."
)

# How many browsers to try for one link, and which ones count as unused.
MAX_LOGIN_TRIES = 6
STALE_AFTER = 60 * 24 * 3600  # a browser whose cookies haven't changed in 60 days isn't tried

LOGIN_TOOLTIP = (
    "Only needed for private videos. Uses the login already saved in your browsers, trying the one "
    "you used most recently first. Your password is never seen or stored."
)

NONE_KEY = "none"
CUSTOM_LABEL = "Custom profile folder…"


@dataclass(frozen=True)
class LoginSource:
    key: str                 # stable id saved in settings
    label: str               # what the dropdown shows
    app_name: str | None     # "Zen", "Chrome"... used in messages ("Close Zen completely")
    ydl_browser: str | None  # "firefox", "chrome", "safari"... or None
    profile: str | None      # profile folder (Firefox family; Chromium: the most recently used profile)
    mtime: float = 0.0
    note: str = ""

    @property
    def is_none(self) -> bool:
        return self.ydl_browser is None

    @property
    def is_chromium(self) -> bool:
        return self.ydl_browser in CHROMIUM_BROWSERS

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


def _chromium_roots(localappdata: Path | None, home: Path | None,
                    platform: str) -> list[tuple[str, str, Path]]:
    if platform == "win32":
        local = localappdata or Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        roaming = Path(os.environ.get("APPDATA") or local.parent / "Roaming")
        roots = [(key, name, local / sub) for key, name, sub in CHROMIUM_WINDOWS]
        roots.append(("opera", "Opera", roaming / "Opera Software" / "Opera Stable"))
        # Arc for Windows is a Store-style app: its folder name has a random suffix.
        packages = local / "Packages"
        try:
            arc = sorted(packages.glob("TheBrowserCompany.Arc_*"))
        except OSError:
            arc = []
        if arc:
            roots.insert(1, ("arc", "Arc", arc[0] / "LocalCache" / "Local" / "Arc" / "User Data"))
        return roots
    home = home or Path.home()
    if platform == "darwin":
        base = home / "Library" / "Application Support"
        return [(key, name, base / sub) for key, name, sub in CHROMIUM_MAC]
    base = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
    return [(key, name, base / sub) for key, name, sub in CHROMIUM_LINUX]


def _newest_cookie_db(user_data: Path) -> tuple[Path, float] | None:
    """(profile folder, mtime) of the most recently written Chromium ``Cookies`` file."""
    best: tuple[Path, float] | None = None
    try:
        folders = [user_data, *(c for c in user_data.iterdir() if c.is_dir())]
    except OSError:
        return None
    for folder in folders:
        for db in (folder / "Network" / "Cookies", folder / "Cookies"):
            try:
                mtime = db.stat().st_mtime
            except OSError:
                continue
            if best is None or mtime > best[1]:
                best = (folder, mtime)
    return best


def detect_chromium(localappdata: Path | None = None, *, home: Path | None = None,
                    platform: str | None = None) -> list[LoginSource]:
    """Chromium-family browsers that have a cookie database, with their most recently used profile.

    On Windows they're marked "(may not work)": Chrome's encryption usually blocks reading them.
    """
    platform = platform or ("win32" if localappdata is not None else sys.platform)
    windows = platform == "win32"
    found: list[LoginSource] = []
    for key, name, user_data in _chromium_roots(localappdata, home, platform):
        newest = _newest_cookie_db(user_data)
        if newest is None:
            continue
        folder, mtime = newest
        # Passing the profile folder (rather than letting yt-dlp search the whole user-data
        # folder, caches and all) keeps the read fast.
        profile = None if key in _NO_PROFILES or folder == user_data else str(folder)
        found.append(LoginSource(
            key=key,
            label=f"{name} (may not work)" if windows else name,
            app_name=name,
            ydl_browser=key,
            profile=profile,
            mtime=mtime,
            note=CHROMIUM_NOTE if windows else "",
        ))
    return found


SAFARI_COOKIES = (
    Path("Library") / "Containers" / "com.apple.Safari" / "Data" / "Library" / "Cookies" / "Cookies.binarycookies",
    Path("Library") / "Cookies" / "Cookies.binarycookies",
)


def detect_safari(home: Path | None = None, *, platform: str | None = None) -> list[LoginSource]:
    """Safari on a Mac. Reading it needs Full Disk Access, so its age may be unknown (mtime 0)."""
    if (platform or sys.platform) != "darwin":
        return []
    home = home or Path.home()
    mtime = 0.0
    for rel in SAFARI_COOKIES:
        try:
            mtime = (home / rel).stat().st_mtime
            break
        except OSError:  # missing, or macOS privacy protection
            continue
    return [LoginSource(key="safari", label="Safari", app_name="Safari", ydl_browser="safari",
                        profile=None, mtime=mtime)]


def custom_source(folder: str | os.PathLike) -> LoginSource | None:
    """A user-chosen Firefox-format profile folder, or None if it has no cookie database."""
    p = Path(folder)
    if not (p / COOKIE_DB).is_file():
        return None
    return LoginSource(key=f"custom:{p}", label=f"Custom — {p.name}", app_name="your browser",
                       ydl_browser="firefox", profile=str(p))


def all_sources(appdata: Path | None = None, localappdata: Path | None = None,
                home: Path | None = None) -> list[LoginSource]:
    """None, then detected Firefox-family profiles, then Chromium-family browsers, then Safari."""
    return [NONE_SOURCE, *detect_firefox_profiles(appdata, home), *detect_chromium(localappdata, home=home),
            *detect_safari(home)]


AUTO_KEY = "auto"


def candidates(sources: list[LoginSource], preferred: str | None = None, *, demote: frozenset | set = frozenset(),
               now: float | None = None, limit: int = MAX_LOGIN_TRIES) -> list[LoginSource]:
    """The browser logins to try for a link, in order.

    ``preferred`` (the one that worked for this site before) comes first, then the rest by
    how recently each browser was used. Browsers not used for a while are skipped (so an old,
    forgotten browser doesn't trigger a Mac permission prompt), unless nothing else is left.
    Sources in ``demote`` (already known not to work) go last. Sorting is stable, so ties
    keep the detection order.
    """
    real = [s for s in sources if not s.is_none]
    now = time.time() if now is None else now
    fresh = [s for s in real if not s.mtime or now - s.mtime < STALE_AFTER] or real
    # On Windows, Chrome-based browsers go after the others: reading them rarely works there.
    windows_blocked = sys.platform == "win32"
    ordered = sorted(fresh, key=lambda s: (s.key in demote, windows_blocked and s.is_chromium, -s.mtime))
    pref = next((s for s in real if s.key == preferred), None) if preferred else None
    if pref is not None:
        ordered = [pref, *(s for s in ordered if s.key != pref.key)]
    return ordered[:limit]


def automatic(sources: list[LoginSource]) -> LoginSource:
    """The browser login tried first when nothing else is known: the most recently used one."""
    order = candidates(sources)
    return order[0] if order else NONE_SOURCE


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
