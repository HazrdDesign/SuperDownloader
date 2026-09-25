import json
import os
import sys

import pytest

from app import paths, settings
from app.settings import Settings


@pytest.fixture
def appdata(tmp_path, monkeypatch):
    monkeypatch.setenv("VD_APPDATA", str(tmp_path / "appdata"))
    return tmp_path / "appdata"


def test_defaults_when_missing(appdata):
    s = settings.load()
    assert s == Settings()
    assert s.check_updates_on_launch is True
    assert s.default_quality == "best"
    assert s.login_source == "auto"
    assert s.default_format == "original"


def test_default_save_folder_is_downloads(appdata):
    assert Settings().effective_save_folder() == paths.downloads_dir()


def test_round_trip_persists(appdata, tmp_path):
    s = Settings(save_folder=str(tmp_path), default_quality="720", login_source="firefox:C:\\p",
                 check_updates_on_launch=False, window_geometry="700x500+10+20")
    settings.save(s)
    assert paths.settings_file().is_file()
    assert settings.load() == s
    assert settings.load().effective_save_folder() == tmp_path


def test_settings_path_under_appdata(appdata):
    assert paths.settings_file() == appdata / "settings.json"


def test_app_folder_on_each_platform(monkeypatch, tmp_path):
    monkeypatch.delenv("VD_APPDATA", raising=False)
    monkeypatch.setattr(paths.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(paths.sys, "platform", "darwin")
    assert paths.app_data_dir() == tmp_path / "Library" / "Application Support" / "SuperDownloader"
    monkeypatch.setattr(paths.sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    assert paths.app_data_dir() == tmp_path / "Roaming" / "SuperDownloader"


def test_corrupt_file_falls_back_and_keeps_copy(appdata):
    appdata.mkdir(parents=True)
    paths.settings_file().write_text("{not json", encoding="utf-8")
    assert settings.load() == Settings()
    assert (appdata / "settings.json.bad").exists()


def test_non_object_root(appdata):
    appdata.mkdir(parents=True)
    paths.settings_file().write_text("[1, 2]", encoding="utf-8")
    assert settings.load() == Settings()


def test_wrong_types_and_unknown_keys_ignored(appdata):
    appdata.mkdir(parents=True)
    paths.settings_file().write_text(json.dumps({
        "save_folder": 5, "default_quality": "8k", "check_updates_on_launch": "yes",
        "window_geometry": "1x1", "something_new": True}), encoding="utf-8")
    s = settings.load()
    assert s.save_folder == ""
    assert s.default_quality == "best"
    assert s.check_updates_on_launch is True
    assert s.window_geometry == "1x1"


def test_save_is_atomic_no_temp_left(appdata):
    settings.save(Settings())
    leftovers = [p for p in appdata.iterdir() if p.suffix == ".tmp"]
    assert leftovers == []


def test_validate_folder(tmp_path):
    assert settings.validate_folder(tmp_path) is None
    assert settings.validate_folder(tmp_path / "nope") == "That folder doesn't exist."
    f = tmp_path / "file.txt"
    f.write_text("x")
    assert settings.validate_folder(f) == "That's a file, not a folder."
    assert settings.validate_folder("  ") == "Choose a folder."


@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0, reason="chmod semantics")
def test_validate_folder_not_writable(tmp_path):
    ro = tmp_path / "ro"
    ro.mkdir()
    ro.chmod(0o500)
    try:
        assert "Can't save" in settings.validate_folder(ro)
    finally:
        ro.chmod(0o700)


@pytest.mark.skipif(sys.platform != "win32", reason="Known Folder API is Windows-only")
def test_known_folder_downloads_resolves():
    d = paths.downloads_dir()
    assert d.is_absolute()
    assert d.exists()


def test_version1_settings_are_upgraded(appdata):
    """Settings from the first version: 'none' login becomes automatic, audio becomes a format."""
    appdata.mkdir(parents=True)
    paths.settings_file().write_text(json.dumps({
        "save_folder": "", "default_quality": "audio", "login_source": "none",
        "check_updates_on_launch": True, "window_geometry": ""}), encoding="utf-8")
    s = settings.load()
    assert s.login_source == "auto"
    assert s.default_quality == "best" and s.default_format == "mp3"


def test_explicit_never_login_is_kept_in_version2(appdata):
    settings.save(Settings(login_source="none"))
    assert settings.load().login_source == "none"


def test_recent_projects(appdata):
    s = Settings()
    for p in ["A / 1", "B", "a / 1", "  ", "C"]:
        settings.remember_project(s, p)
    assert s.recent_projects == ["C", "a / 1", "B"]
    for i in range(20):
        settings.remember_project(s, f"P{i}")
    assert len(s.recent_projects) == settings.MAX_RECENT_PROJECTS
    settings.save(s)
    assert settings.load().recent_projects == s.recent_projects


def test_legacy_app_folder_is_migrated(tmp_path, monkeypatch):
    monkeypatch.delenv("VD_APPDATA", raising=False)
    monkeypatch.setattr(paths, "_roaming_root", lambda: tmp_path)  # %APPDATA%, ~/Library/Application Support...
    old = tmp_path / "VideoDownloader"
    (old / "engine").mkdir(parents=True)
    (old / "engine" / "active.json").write_text("{}")
    (old / "settings.json").write_text(json.dumps({"login_source": "firefox:/zen", "version": 2}))
    (old / "logs").mkdir()
    assert paths.migrate_legacy_data() == old
    new = tmp_path / "SuperDownloader"
    assert json.loads((new / "settings.json").read_text())["login_source"] == "firefox:/zen"
    assert (new / "engine" / "active.json").is_file()
    assert not (new / "logs").exists()
    assert paths.migrate_legacy_data() is None  # only once
