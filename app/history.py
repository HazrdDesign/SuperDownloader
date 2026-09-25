"""Download history: one entry per download session, stored in %APPDATA%\\SuperDownloader\\history.json.

Only finished sessions are kept (saved or failed; cancelled downloads are left out).
Video passwords are never stored.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import paths

log = logging.getLogger(__name__)

MAX_ENTRIES = 200


@dataclass
class HistoryEntry:
    url: str
    title: str
    ok: bool
    when: float = field(default_factory=time.time)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    site: str = ""
    quality_key: str = "best"
    format_key: str = "original"
    files: list = field(default_factory=list)       # saved file paths (strings)
    folder: str = ""
    error: str = ""                                  # plain-English reason when it failed
    count: int = 1                                   # videos in this session (playlists)
    failed_count: int = 0
    used_login: bool = False
    referer: str = ""
    project: str = ""
    clip: list = field(default_factory=list)         # [start, end] seconds, or empty
    subtitles: str = ""                              # language code, "" for none
    subtitles_auto: bool = False
    playlist_all: bool = False

    def first_file(self) -> Path | None:
        for f in self.files:
            p = Path(f)
            if p.exists():
                return p
        return None


def _file() -> Path:
    return paths.app_data_dir() / "history.json"


def load() -> list[HistoryEntry]:
    try:
        data = json.loads(_file().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as e:
        log.warning("History file unreadable (%s); starting a new one", e)
        return []
    names = {f.name for f in dataclasses.fields(HistoryEntry)}
    out = []
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict) or "url" not in item:
            continue
        try:
            out.append(HistoryEntry(**{k: v for k, v in item.items() if k in names}))
        except TypeError:
            continue
    return out[:MAX_ENTRIES]


def save(entries: list[HistoryEntry]) -> None:
    f = _file()
    f.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps([dataclasses.asdict(e) for e in entries[:MAX_ENTRIES]], indent=1, ensure_ascii=False)
    fd, tmp = tempfile.mkstemp(dir=f.parent, prefix=".history-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.replace(tmp, f)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class History:
    """In-memory history, newest first, saved on every change."""

    def __init__(self, entries: list[HistoryEntry] | None = None, persist: bool = True):
        self.entries = entries if entries is not None else (load() if persist else [])
        self._persist = persist

    def _save(self) -> None:
        if self._persist:
            try:
                save(self.entries)
            except OSError as e:
                log.warning("Could not save history: %s", e)

    def add(self, entry: HistoryEntry) -> None:
        self.entries.insert(0, entry)
        del self.entries[MAX_ENTRIES:]
        self._save()

    def remove(self, entry_id: str) -> None:
        self.entries = [e for e in self.entries if e.id != entry_id]
        self._save()

    def clear(self) -> None:
        self.entries = []
        self._save()


def relative_time(when: float, now: float | None = None) -> str:
    now = now or time.time()
    d = max(0, int(now - when))
    if d < 60:
        return "just now"
    if d < 3600:
        return f"{d // 60} min ago"
    if d < 86400:
        return f"{d // 3600} h ago"
    days = d // 86400
    if days == 1:
        return "yesterday"
    if days < 7:
        return f"{days} days ago"
    return time.strftime("%d %b %Y", time.localtime(when))
