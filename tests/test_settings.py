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
    assert s.login_source == "none"


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
