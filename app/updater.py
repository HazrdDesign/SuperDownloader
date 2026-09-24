"""Keep the downloader engine (yt-dlp) up to date inside a frozen .exe.

A PyInstaller exe can't pip-install, so updates work like this:

1. ``check_for_update`` asks PyPI for the latest yt-dlp release and the exact
   yt-dlp-ejs version it pins (needed for full YouTube support).
2. ``install_update`` downloads both pure-Python wheels into
   ``%APPDATA%\\VideoDownloader\\engine\\``, verifies their SHA-256 against PyPI,
   and writes ``active.json``.
3. On the next launch ``bootstrap_engine`` (called before anything imports
   yt_dlp) puts those wheels at the front of ``sys.path``. Python imports
   straight from the .whl (it is a zip). If anything goes wrong it falls back
   to the version bundled in the exe.

This module must not import yt_dlp at module level.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.metadata
import json
import logging
import os
import re
import sys
import tempfile
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import paths

log = logging.getLogger(__name__)

PYPI_JSON = "https://pypi.org/pypi/{name}/json"
PYPI_VERSION_JSON = "https://pypi.org/pypi/{name}/{version}/json"
MANIFEST = "active.json"

ENGINE = "yt-dlp"
EJS = "yt-dlp-ejs"
# distribution name -> top-level import name
PACKAGES = {ENGINE: "yt_dlp", EJS: "yt_dlp_ejs"}

Opener = Callable[..., object]


def version_tuple(v: str | None) -> tuple[int, ...]:
    """'2026.08.19' and '2026.8.19' compare equal; '2026.8.19.1' > '2026.8.19'."""
    if not v:
        return ()
    return tuple(int(x) for x in re.findall(r"\d+", v.split("+")[0]))


@dataclass
class Wheel:
    name: str
    version: str
    url: str
    sha256: str
    filename: str


@dataclass
class UpdateInfo:
    current: str
    latest: str
    wheels: list[Wheel] = field(default_factory=list)

    @property
    def available(self) -> bool:
        return version_tuple(self.latest) > version_tuple(self.current)


@dataclass
class EngineState:
    version: str | None = None
    source: str = "bundled"      # "bundled" or "updated"
    bundled_version: str | None = None
    error: str | None = None


STATE = EngineState()


# --------------------------------------------------------------------------------------------
# Manifest
# --------------------------------------------------------------------------------------------

def _manifest_path(engine_dir: Path) -> Path:
    return engine_dir / MANIFEST


def read_manifest(engine_dir: Path | None = None) -> dict:
    engine_dir = engine_dir or paths.engine_dir()
    try:
        data = json.loads(_manifest_path(engine_dir).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_manifest(engine_dir: Path, data: dict) -> None:
    engine_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=engine_dir, prefix=".manifest-", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, _manifest_path(engine_dir))


def installed_version(engine_dir: Path | None = None) -> str | None:
    """Version of the downloaded engine (active now or after restart), if any."""
    m = read_manifest(engine_dir)
    if m.get("failed"):
        return None
    return (m.get("packages") or {}).get(ENGINE, {}).get("version")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# --------------------------------------------------------------------------------------------
# Startup
# --------------------------------------------------------------------------------------------

def _dist_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def _purge(import_name: str) -> None:
    for mod in list(sys.modules):
        if mod == import_name or mod.startswith(import_name + "."):
            del sys.modules[mod]


def bootstrap_engine(engine_dir: Path | None = None, packages: dict[str, str] | None = None,
                     allow_older: bool = False) -> EngineState:
    """Activate a downloaded engine if it is valid and newer than the bundled one.

    Must run before anything imports yt_dlp. Always leaves a working engine
    importable (the bundled one if the download is unusable).
    """
    engine_dir = engine_dir or paths.engine_dir()
    packages = packages or PACKAGES
    # CI uses this to prove the frozen exe can load a downloaded engine of the same version.
    allow_older = allow_older or os.environ.get("VD_ENGINE_ALLOW_OLDER") == "1"
    main_dist = next(iter(packages))
    main_import = packages[main_dist]
    bundled = _dist_version(main_dist)
    state = EngineState(version=bundled, bundled_version=bundled)

    manifest = read_manifest(engine_dir)
    entries = manifest.get("packages") or {}
    if not entries or manifest.get("failed"):
        return _finish(state, main_import)

    wanted = entries.get(main_dist, {}).get("version")
    if not allow_older and bundled and version_tuple(wanted) <= version_tuple(bundled):
        log.info("Bundled engine %s is as new as downloaded %s; using bundled", bundled, wanted)
        return _finish(state, main_import)

    wheel_paths: list[str] = []
    for dist in packages:
        entry = entries.get(dist)
        if not entry:
            state.error = f"downloaded engine is missing {dist}"
            return _finish(state, main_import)
        wheel = engine_dir / entry.get("file", "")
        if not wheel.is_file() or _sha256(wheel) != entry.get("sha256"):
            state.error = f"downloaded {dist} failed its integrity check"
            log.warning("Engine update ignored: %s", state.error)
            return _finish(state, main_import)
        wheel_paths.append(str(wheel))

    for p in reversed(wheel_paths):
        sys.path.insert(0, p)
    try:
        for dist, import_name in packages.items():
            _purge(import_name)
            mod = importlib.import_module(import_name)
            origin = os.path.normcase(str(getattr(mod, "__file__", "") or ""))
            if not any(origin.startswith(os.path.normcase(p)) for p in wheel_paths):
                raise ImportError(f"{import_name} was not loaded from the downloaded engine ({origin})")
        _self_test(main_import, wanted)
    except Exception as e:  # noqa: BLE001 - any failure means "use the bundled engine"
        log.warning("Downloaded engine %s failed to load (%s); falling back to bundled %s", wanted, e, bundled)
        for p in wheel_paths:
            while p in sys.path:
                sys.path.remove(p)
        for import_name in packages.values():
            _purge(import_name)
        importlib.invalidate_caches()
        manifest["failed"] = True
        manifest["failed_version"] = wanted
        try:
            _write_manifest(engine_dir, manifest)
        except OSError:
            pass
        state.error = str(e)
        return _finish(state, main_import)

    state.version = wanted
    state.source = "updated"
    log.info("Using downloaded engine %s (bundled %s)", wanted, bundled)
    return _finish(state, main_import)


def _self_test(import_name: str, expected_version: str | None) -> None:
    mod = importlib.import_module(import_name)
    if import_name == "yt_dlp":
        from yt_dlp import YoutubeDL  # type: ignore  # noqa: F401  (imported from the wheel)
        with mod.YoutubeDL({"quiet": True, "simulate": True}):
            pass
        actual = mod.version.__version__
    else:
        actual = getattr(mod, "__version__", None) or getattr(getattr(mod, "version", None), "__version__", None)
    if expected_version and actual and version_tuple(actual) != version_tuple(expected_version):
        raise ImportError(f"expected version {expected_version}, got {actual}")


def _finish(state: EngineState, main_import: str) -> EngineState:
    global STATE
    if state.source == "bundled":
        try:
            mod = importlib.import_module(main_import)
            ver = getattr(getattr(mod, "version", None), "__version__", None) or getattr(mod, "__version__", None)
            state.version = ver or state.version
        except Exception as e:  # noqa: BLE001
            log.error("Bundled engine failed to import: %s", e)
            state.error = state.error or str(e)
    STATE = state
    return state


# --------------------------------------------------------------------------------------------
# Checking and installing
# --------------------------------------------------------------------------------------------

def _get_json(url: str, opener: Opener | None, timeout: float) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "VideoDownloader-updater",
                                               "Accept": "application/json"})
    with (opener or urllib.request.urlopen)(req, timeout=timeout) as resp:  # type: ignore[operator]
        return json.loads(resp.read().decode("utf-8"))


def _pure_wheel(data: dict, name: str) -> Wheel:
    version = data["info"]["version"]
    for f in data.get("urls", []):
        fn = f.get("filename", "")
        if f.get("packagetype") == "bdist_wheel" and fn.endswith("-none-any.whl") and "py3" in fn:
            return Wheel(name, version, f["url"], f["digests"]["sha256"], fn)
    raise ValueError(f"no pure-Python wheel for {name} {version}")


def _pinned_ejs(requires_dist: list[str] | None) -> str | None:
    for req in requires_dist or []:
        m = re.match(r"^\s*yt-dlp-ejs\s*==\s*([\w.]+)", req)
        if m:
            return m.group(1)
    return None


def check_for_update(current: str | None = None, *, opener: Opener | None = None,
                     timeout: float = 15, engine_dir: Path | None = None) -> UpdateInfo:
    """Ask PyPI for the newest yt-dlp. Raises on network problems (callers classify)."""
    engine_dir = engine_dir or paths.engine_dir()
    current = current or STATE.version or "0"
    pending = installed_version(engine_dir)
    if pending and version_tuple(pending) > version_tuple(current):
        current = pending  # already downloaded, waiting for a restart

    data = _get_json(PYPI_JSON.format(name=ENGINE), opener, timeout)
    engine = _pure_wheel(data, ENGINE)
    info = UpdateInfo(current=current, latest=engine.version)

    failed = read_manifest(engine_dir).get("failed_version")
    if failed and version_tuple(failed) == version_tuple(engine.version):
        log.info("Latest engine %s failed to load before; not offering it again", failed)
        info.latest = current
        return info
    if not info.available:
        return info

    ejs_version = _pinned_ejs(data["info"].get("requires_dist"))
    ejs_url = (PYPI_VERSION_JSON.format(name=EJS, version=ejs_version) if ejs_version
               else PYPI_JSON.format(name=EJS))
    ejs = _pure_wheel(_get_json(ejs_url, opener, timeout), EJS)
    info.wheels = [engine, ejs]
    return info


def install_update(info: UpdateInfo, *, opener: Opener | None = None, timeout: float = 60,
                   engine_dir: Path | None = None) -> str:
    """Download and verify the wheels, then point ``active.json`` at them. Returns the version."""
    engine_dir = engine_dir or paths.engine_dir()
    engine_dir.mkdir(parents=True, exist_ok=True)
    entries: dict[str, dict] = {}
    for wheel in info.wheels:
        target = engine_dir / wheel.filename
        if not (target.is_file() and _sha256(target) == wheel.sha256):
            fd, tmp = tempfile.mkstemp(dir=engine_dir, prefix=".download-", suffix=".tmp")
            try:
                h = hashlib.sha256()
                req = urllib.request.Request(wheel.url, headers={"User-Agent": "VideoDownloader-updater"})
                with os.fdopen(fd, "wb") as out, (opener or urllib.request.urlopen)(req, timeout=timeout) as resp:  # type: ignore[operator]
                    while chunk := resp.read(1 << 16):
                        h.update(chunk)
                        out.write(chunk)
                if h.hexdigest() != wheel.sha256:
                    raise ValueError(f"checksum mismatch for {wheel.filename}")
                import_name = PACKAGES.get(wheel.name, wheel.name.replace("-", "_"))
                with zipfile.ZipFile(tmp) as z:
                    if not any(n.startswith(import_name + "/") for n in z.namelist()):
                        raise ValueError(f"{wheel.filename} does not contain {import_name}")
                os.replace(tmp, target)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        entries[wheel.name] = {"version": wheel.version, "file": wheel.filename, "sha256": wheel.sha256}

    _write_manifest(engine_dir, {"packages": entries})
    keep = {e["file"] for e in entries.values()} | {MANIFEST}
    for old in engine_dir.iterdir():
        if old.name not in keep and old.suffix == ".whl":
            try:
                old.unlink()
            except OSError:
                pass  # in use by this process; removed after the next update
    log.info("Engine %s downloaded; active after restart", info.latest)
    return info.latest
