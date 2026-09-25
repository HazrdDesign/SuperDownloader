import os
import sys
import time
from pathlib import Path

import pytest

from app import browsers
from app.browsers import NONE_SOURCE, LoginSource


def make_profile(root: Path, folder: str, mtime: float | None = None, with_db: bool = True) -> Path:
    p = root / "Profiles" / folder
    p.mkdir(parents=True)
    if with_db:
        db = p / "cookies.sqlite"
        db.write_bytes(b"SQLite format 3\x00")
        if mtime is not None:
            os.utime(db, (mtime, mtime))
            os.utime(p, (mtime, mtime))
    return p


def write_ini(root: Path, entries: list[tuple[str, str, bool]]):
    lines = []
    for i, (name, path, relative) in enumerate(entries):
        lines += [f"[Profile{i}]", f"Name={name}", f"IsRelative={1 if relative else 0}", f"Path={path}", ""]
    lines += ["[General]", "StartWithLastProfile=1"]
    (root / "profiles.ini").write_text("\n".join(lines), encoding="utf-8")


def test_detects_all_four_families_and_labels(tmp_path):
    appdata = tmp_path / "Roaming"
    now = time.time()
    ff = appdata / "Mozilla" / "Firefox"
    make_profile(ff, "abcd1234.default-release", now - 300)
    zen = appdata / "zen"
    make_profile(zen, "q1w2e3r4.Default (release)", now - 10)
    write_ini(zen, [("Default (release)", "Profiles/q1w2e3r4.Default (release)", True)])
    make_profile(appdata / "librewolf", "zzzz.default-default", now - 200)
    make_profile(appdata / "Floorp", "ffff.default-release", now - 100)

    found = browsers.detect_firefox_profiles(appdata=appdata)
    labels = [s.label for s in found]
    assert labels == [
        "Zen — Default (release)",       # most recent first
        "Floorp — default-release",
        "LibreWolf — default-default",
        "Firefox — default-release",
    ]
    assert all(s.ydl_browser == "firefox" for s in found)


def test_profile_without_cookie_db_is_skipped(tmp_path):
    appdata = tmp_path / "Roaming"
    make_profile(appdata / "Mozilla" / "Firefox", "empty.default", with_db=False)
    assert browsers.detect_firefox_profiles(appdata=appdata) == []


def test_missing_roots_are_fine(tmp_path):
    assert browsers.detect_firefox_profiles(appdata=tmp_path / "nothing-here") == []


def test_cookiesfrombrowser_tuple(tmp_path):
    appdata = tmp_path / "Roaming"
    p = make_profile(appdata / "zen", "x.Default (release)")
    [src] = browsers.detect_firefox_profiles(appdata=appdata)
    assert src.cookiesfrombrowser() == ("firefox", str(p), None, None)
    assert src.app_name == "Zen"
    assert NONE_SOURCE.cookiesfrombrowser() is None


def test_absolute_path_in_profiles_ini(tmp_path):
    appdata = tmp_path / "Roaming"
    root = appdata / "Mozilla" / "Firefox"
    p = make_profile(root, "abs.default")
    write_ini(root, [("Work", str(p), False)])
    [src] = browsers.detect_firefox_profiles(appdata=appdata)
    assert src.label == "Firefox — Work"


def test_duplicate_labels_get_folder_suffix(tmp_path):
    appdata = tmp_path / "Roaming"
    root = appdata / "Mozilla" / "Firefox"
    make_profile(root, "aaa.default", time.time() - 5)
    make_profile(root, "bbb.default", time.time())
    labels = [s.label for s in browsers.detect_firefox_profiles(appdata=appdata)]
    assert labels == ["Firefox — default (bbb.default)", "Firefox — default (aaa.default)"]


def test_broken_profiles_ini_is_ignored(tmp_path):
    appdata = tmp_path / "Roaming"
    root = appdata / "zen"
    make_profile(root, "k.Default (release)")
    (root / "profiles.ini").write_bytes(b"\xff\xfe garbage [[[")
    [src] = browsers.detect_firefox_profiles(appdata=appdata)
    assert src.label == "Zen — Default (release)"


def make_chromium(user_data: Path, profile: str = "Default", mtime: float | None = None,
                  network: bool = True) -> Path:
    folder = user_data / profile
    db = folder / "Network" / "Cookies" if network else folder / "Cookies"
    db.parent.mkdir(parents=True, exist_ok=True)
    db.write_bytes(b"SQLite format 3\x00")
    if mtime is not None:
        os.utime(db, (mtime, mtime))
    return folder


def test_chromium_detected_on_windows_with_note(tmp_path):
    local = tmp_path / "Local"
    make_chromium(local / "Google" / "Chrome" / "User Data")
    make_chromium(local / "Microsoft" / "Edge" / "User Data")
    (local / "BraveSoftware" / "Brave-Browser" / "User Data").mkdir(parents=True)  # installed, never used
    found = browsers.detect_chromium(localappdata=local)
    assert [s.ydl_browser for s in found] == ["chrome", "edge"]
    assert all(s.is_chromium and "Windows encryption" in s.note and "(may not work)" in s.label for s in found)
    # The profile folder is passed so yt-dlp doesn't search the whole user-data folder.
    profile = str(local / "Google" / "Chrome" / "User Data" / "Default")
    assert found[0].cookiesfrombrowser() == ("chrome", profile, None, None)


def test_chromium_newest_profile_is_used(tmp_path):
    local = tmp_path / "Local"
    ud = local / "Google" / "Chrome" / "User Data"
    now = time.time()
    make_chromium(ud, "Default", now - 1000)
    make_chromium(ud, "Profile 2", now - 5, network=False)
    [chrome] = browsers.detect_chromium(localappdata=local)
    assert chrome.profile == str(ud / "Profile 2")
    assert abs(chrome.mtime - (now - 5)) < 1


def test_arc_found_on_windows(tmp_path):
    local = tmp_path / "Local"
    make_chromium(local / "Packages" / "TheBrowserCompany.Arc_ttt1ap7aakyb4" / "LocalCache" / "Local" / "Arc"
                  / "User Data")
    [arc] = browsers.detect_chromium(localappdata=local)
    assert (arc.key, arc.app_name, arc.ydl_browser) == ("arc", "Arc", "arc")


def test_mac_browsers(tmp_path):
    """On a Mac: Chrome, Arc and Safari, with no Windows note (their logins can be read there)."""
    base = tmp_path / "Library" / "Application Support"
    make_chromium(base / "Google" / "Chrome", network=False)
    make_chromium(base / "Arc" / "User Data")
    found = browsers.detect_chromium(home=tmp_path, platform="darwin")
    assert [(s.app_name, s.label, s.note) for s in found] == [("Chrome", "Chrome", ""), ("Arc", "Arc", "")]
    [safari] = browsers.detect_safari(tmp_path, platform="darwin")
    assert safari.ydl_browser == "safari" and safari.mtime == 0  # unknown until Full Disk Access
    cookies = tmp_path / browsers.SAFARI_COOKIES[0]
    cookies.parent.mkdir(parents=True)
    cookies.write_bytes(b"cook")
    assert browsers.detect_safari(tmp_path, platform="darwin")[0].mtime > 0
    assert browsers.detect_safari(tmp_path, platform="win32") == []


@pytest.mark.skipif(sys.platform == "win32", reason="Mac/Linux folder layout")
def test_mac_firefox_family(tmp_path):
    make_profile(tmp_path / "Library" / "Application Support" / "zen", "x.Default")
    [zen] = browsers.detect_firefox_profiles(home=tmp_path)
    assert zen.app_name == "Zen"


def src(key, mtime, browser="firefox", name=None):
    return LoginSource(key, key, name or key.title(), browser, None, mtime)


def test_candidates_most_recent_first(monkeypatch):
    monkeypatch.setattr(browsers.sys, "platform", "darwin")
    now = 1_000_000_000.0
    zen, chrome, arc = src("zen", now - 50), src("chrome", now - 10, "chrome"), src("arc", now - 20, "arc")
    safari = src("safari", 0, "safari")
    order = browsers.candidates([NONE_SOURCE, zen, chrome, arc, safari], now=now)
    assert [s.key for s in order] == ["chrome", "arc", "zen", "safari"]  # unknown age last
    # The browser that worked for this site before goes first.
    assert browsers.candidates([zen, chrome, arc], "zen", now=now)[0] == zen
    # Known-unreadable ones go last.
    assert [s.key for s in browsers.candidates([zen, chrome, arc], demote={"chrome"}, now=now)] == ["arc", "zen",
                                                                                                    "chrome"]
    assert browsers.automatic([NONE_SOURCE, zen, chrome]) == chrome
    assert browsers.automatic([NONE_SOURCE]) is NONE_SOURCE


def test_candidates_skip_unused_browsers_and_cap(monkeypatch):
    monkeypatch.setattr(browsers.sys, "platform", "darwin")
    now = 1_000_000_000.0
    old = src("opera", now - browsers.STALE_AFTER - 1, "opera")
    fresh = src("chrome", now - 5, "chrome")
    assert browsers.candidates([old, fresh], now=now) == [fresh]
    assert browsers.candidates([old], now=now) == [old]  # nothing else: still try it
    many = [src(f"p{i}", now - i) for i in range(10)]
    assert len(browsers.candidates(many, now=now)) == browsers.MAX_LOGIN_TRIES


def test_candidates_on_windows_try_chrome_based_last(monkeypatch):
    monkeypatch.setattr(browsers.sys, "platform", "win32")
    now = 1_000_000_000.0
    zen, chrome = src("zen", now - 500), src("chrome", now - 1, "chrome")
    assert browsers.candidates([chrome, zen], now=now) == [zen, chrome]


def test_all_sources_order(tmp_path):
    appdata, local = tmp_path / "Roaming", tmp_path / "Local"
    make_profile(appdata / "zen", "a.Default (release)")
    make_chromium(local / "BraveSoftware" / "Brave-Browser" / "User Data")
    srcs = browsers.all_sources(appdata=appdata, localappdata=local)
    assert srcs[0] is NONE_SOURCE
    assert srcs[1].app_name == "Zen"
    assert srcs[2].ydl_browser == "brave"


def test_custom_source(tmp_path):
    assert browsers.custom_source(tmp_path) is None
    (tmp_path / "cookies.sqlite").write_bytes(b"x")
    src = browsers.custom_source(tmp_path)
    assert src.cookiesfrombrowser() == ("firefox", str(tmp_path), None, None)
    assert src.key.startswith("custom:")


def test_from_key_round_trip_and_fallbacks(tmp_path):
    appdata = tmp_path / "Roaming"
    p = make_profile(appdata / "zen", "a.Default (release)")
    srcs = browsers.all_sources(appdata=appdata, localappdata=tmp_path / "L")
    zen = srcs[1]
    assert browsers.from_key(zen.key, srcs) == zen
    assert browsers.from_key("none", srcs) is NONE_SOURCE
    assert browsers.from_key(None, srcs) is NONE_SOURCE
    assert browsers.from_key("chrome", srcs) is NONE_SOURCE  # not installed
    assert browsers.from_key(f"firefox:{tmp_path / 'gone'}", srcs) is NONE_SOURCE
    custom = browsers.from_key(f"custom:{p}", srcs)
    assert custom.profile == str(p)


def test_login_source_is_hashable_and_frozen():
    s = LoginSource("k", "l", "A", "firefox", "/p")
    assert {s: 1}[s] == 1
