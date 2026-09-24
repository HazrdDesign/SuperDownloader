"""Load and save user settings as JSON in ``%APPDATA%\\VideoDownloader\\settings.json``."""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import paths

log = logging.getLogger(__name__)

QUALITY_CHOICES = ("best", "1080", "720", "audio")
QUALITY_LABELS = {
    "best": "Best available",
    "1080": "1080p",
    "720": "720p",
    "audio": "Audio only (MP3)",
}


@dataclass
class Settings:
    # Empty string means "the user's Downloads folder", resolved fresh each run so
    # a moved Downloads folder is followed automatically.
    save_folder: str = ""
    default_quality: str = "best"
    # A login source key from browsers.py ("none", "firefox:<profile path>", "chrome", ...).
    login_source: str = "none"
    check_updates_on_launch: bool = True
    window_geometry: str = ""

    def effective_save_folder(self) -> Path:
        if self.save_folder:
            return Path(self.save_folder)
        return paths.downloads_dir()


def _coerce(data: dict) -> Settings:
    defaults = Settings()
    values = {}
    for f in dataclasses.fields(Settings):
        if f.name not in data:
            continue
        value = data[f.name]
        expected = type(getattr(defaults, f.name))
        if isinstance(value, expected):
            values[f.name] = value
        else:
            log.warning("Ignoring setting %s with unexpected type %s", f.name, type(value).__name__)
    s = Settings(**values)
    if s.default_quality not in QUALITY_CHOICES:
        s.default_quality = defaults.default_quality
    return s


def load(path: Path | None = None) -> Settings:
    path = path or paths.settings_file()
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return Settings()
    except OSError as e:
        log.warning("Could not read settings (%s); using defaults", e)
        return Settings()
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("settings root is not an object")
    except ValueError as e:
        log.warning("Settings file is corrupt (%s); keeping a copy and using defaults", e)
        try:
            os.replace(path, path.with_suffix(".json.bad"))
        except OSError:
            pass
        return Settings()
    return _coerce(data)


def save(settings: Settings, path: Path | None = None) -> None:
    """Write settings atomically (temp file + rename) so a crash can't leave a half-written file."""
    path = path or paths.settings_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(dataclasses.asdict(settings), indent=2, ensure_ascii=False)
    fd, tmp = tempfile.mkstemp(prefix="settings.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def validate_folder(folder: str | os.PathLike) -> str | None:
    """Return None if ``folder`` exists and is writable, otherwise a plain-English problem."""
    if not str(folder).strip():
        return "Choose a folder."
    p = Path(folder)
    if not p.exists():
        return "That folder doesn't exist."
    if not p.is_dir():
        return "That's a file, not a folder."
    try:
        with tempfile.NamedTemporaryFile(dir=p, prefix=".vd-write-test-", delete=True):
            pass
    except OSError:
        return "Can't save files in that folder. Choose a different one."
    return None
