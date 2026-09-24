import os
import time
from pathlib import Path

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


def test_chromium_detected_with_note(tmp_path):
    local = tmp_path / "Local"
    (local / "Google" / "Chrome" / "User Data").mkdir(parents=True)
    (local / "Microsoft" / "Edge" / "User Data").mkdir(parents=True)
    found = browsers.detect_chromium(localappdata=local)
    assert [s.ydl_browser for s in found] == ["chrome", "edge"]
    assert all(s.is_chromium and "Windows encryption" in s.note for s in found)
    assert found[0].cookiesfrombrowser() == ("chrome", None, None, None)


def test_all_sources_order(tmp_path):
    appdata, local = tmp_path / "Roaming", tmp_path / "Local"
    make_profile(appdata / "zen", "a.Default (release)")
    (local / "BraveSoftware" / "Brave-Browser" / "User Data").mkdir(parents=True)
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
