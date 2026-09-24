import hashlib
import io
import json
import os
import subprocess
import sys
import textwrap
import zipfile
from pathlib import Path

import pytest

from app import updater
from app.updater import UpdateInfo, Wheel, version_tuple

ROOT = Path(__file__).resolve().parent.parent
network = pytest.mark.skipif(os.environ.get("VD_NETWORK_TESTS") != "1",
                             reason="set VD_NETWORK_TESTS=1 to run tests that reach PyPI")


def make_wheel(path: Path, package: str, init_source: str) -> str:
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(f"{package}/__init__.py", init_source)
        z.writestr(f"{package}-0.dist-info/METADATA", f"Name: {package}\nVersion: 0\n")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_manifest(engine_dir: Path, entries: dict, **extra):
    (engine_dir / "active.json").write_text(json.dumps({"packages": entries, **extra}), encoding="utf-8")


# ---- versions ----------------------------------------------------------------------------------

def test_version_tuple():
    assert version_tuple("2026.08.19") == version_tuple("2026.8.19") == (2026, 8, 19)
    assert version_tuple("2026.8.19.1") > version_tuple("2026.8.19")
    assert version_tuple("2026.9.1") > version_tuple("2026.8.30")
    assert version_tuple(None) == ()


def test_update_info_available():
    assert UpdateInfo("2026.08.19", "2026.9.1").available
    assert not UpdateInfo("2026.08.19", "2026.8.19").available


# ---- check_for_update with a fake PyPI ----------------------------------------------------------

class FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def fake_pypi(responses: dict[str, bytes]):
    calls = []

    def opener(req, timeout=None):
        url = req.full_url
        calls.append(url)
        if url not in responses:
            raise OSError(f"unexpected URL {url}")
        return FakeResp(responses[url])

    opener.calls = calls
    return opener


def pypi_doc(name, version, filename, sha, requires=None):
    return json.dumps({
        "info": {"version": version, "requires_dist": requires or []},
        "urls": [
            {"packagetype": "sdist", "filename": f"{name}-{version}.tar.gz", "url": "https://x/sdist",
             "digests": {"sha256": "0"}},
            {"packagetype": "bdist_wheel", "filename": filename, "url": f"https://files.example/{filename}",
             "digests": {"sha256": sha}},
        ],
    }).encode()


def test_check_for_update_finds_newer_and_pinned_ejs(tmp_path):
    opener = fake_pypi({
        updater.PYPI_JSON.format(name="yt-dlp"): pypi_doc(
            "yt_dlp", "2026.9.20", "yt_dlp-2026.9.20-py3-none-any.whl", "aa",
            requires=['yt-dlp-ejs==0.9.1; extra == "default"']),
        updater.PYPI_VERSION_JSON.format(name="yt-dlp-ejs", version="0.9.1"): pypi_doc(
            "yt_dlp_ejs", "0.9.1", "yt_dlp_ejs-0.9.1-py3-none-any.whl", "bb"),
    })
    info = updater.check_for_update("2026.08.19", opener=opener, engine_dir=tmp_path)
    assert info.available
    assert info.latest == "2026.9.20"
    assert [(w.name, w.version, w.sha256) for w in info.wheels] == [
        ("yt-dlp", "2026.9.20", "aa"), ("yt-dlp-ejs", "0.9.1", "bb")]


def test_check_for_update_up_to_date(tmp_path):
    opener = fake_pypi({updater.PYPI_JSON.format(name="yt-dlp"): pypi_doc(
        "yt_dlp", "2026.8.19", "yt_dlp-2026.8.19-py3-none-any.whl", "aa")})
    info = updater.check_for_update("2026.08.19", opener=opener, engine_dir=tmp_path)
    assert not info.available
    assert info.wheels == []
    assert len(opener.calls) == 1


def test_check_counts_pending_download_as_current(tmp_path):
    write_manifest(tmp_path, {"yt-dlp": {"version": "2026.9.20", "file": "x.whl", "sha256": "0"}})
    opener = fake_pypi({updater.PYPI_JSON.format(name="yt-dlp"): pypi_doc(
        "yt_dlp", "2026.9.20", "yt_dlp-2026.9.20-py3-none-any.whl", "aa")})
    info = updater.check_for_update("2026.08.19", opener=opener, engine_dir=tmp_path)
    assert not info.available


def test_check_does_not_reoffer_a_version_that_failed(tmp_path):
    write_manifest(tmp_path, {}, failed=True, failed_version="2026.9.20")
    opener = fake_pypi({updater.PYPI_JSON.format(name="yt-dlp"): pypi_doc(
        "yt_dlp", "2026.9.20", "yt_dlp-2026.9.20-py3-none-any.whl", "aa")})
    assert not updater.check_for_update("2026.08.19", opener=opener, engine_dir=tmp_path).available


def test_check_raises_on_network_error(tmp_path):
    with pytest.raises(OSError):
        updater.check_for_update("1", opener=fake_pypi({}), engine_dir=tmp_path)


# ---- install_update ----------------------------------------------------------------------------

def test_install_verifies_checksum_and_writes_manifest(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    sha_a = make_wheel(src / "a.whl", "yt_dlp", "")
    sha_b = make_wheel(src / "b.whl", "yt_dlp_ejs", "")
    engine_dir = tmp_path / "engine"
    engine_dir.mkdir()
    (engine_dir / "yt_dlp-old-py3-none-any.whl").write_bytes(b"old")
    info = UpdateInfo("1", "2", [
        Wheel("yt-dlp", "2", "https://f/a.whl", sha_a, "yt_dlp-2-py3-none-any.whl"),
        Wheel("yt-dlp-ejs", "0.9", "https://f/b.whl", sha_b, "yt_dlp_ejs-0.9-py3-none-any.whl"),
    ])
    opener = fake_pypi({"https://f/a.whl": (src / "a.whl").read_bytes(),
                        "https://f/b.whl": (src / "b.whl").read_bytes()})
    assert updater.install_update(info, opener=opener, engine_dir=engine_dir) == "2"
    manifest = updater.read_manifest(engine_dir)
    assert manifest["packages"]["yt-dlp"] == {"version": "2", "file": "yt_dlp-2-py3-none-any.whl", "sha256": sha_a}
    assert updater.installed_version(engine_dir) == "2"
    assert sorted(p.name for p in engine_dir.iterdir()) == [
        "active.json", "yt_dlp-2-py3-none-any.whl", "yt_dlp_ejs-0.9-py3-none-any.whl"]


def test_install_rejects_bad_checksum(tmp_path):
    src = tmp_path / "a.whl"
    make_wheel(src, "yt_dlp", "")
    info = UpdateInfo("1", "2", [Wheel("yt-dlp", "2", "https://f/a.whl", "deadbeef", "yt_dlp-2-py3-none-any.whl")])
    engine_dir = tmp_path / "engine"
    with pytest.raises(ValueError, match="checksum"):
        updater.install_update(info, opener=fake_pypi({"https://f/a.whl": src.read_bytes()}), engine_dir=engine_dir)
    assert not (engine_dir / "active.json").exists()
    assert [p for p in engine_dir.iterdir()] == []  # temp download removed


def test_install_rejects_wheel_without_package(tmp_path):
    src = tmp_path / "a.whl"
    sha = make_wheel(src, "something_else", "")
    info = UpdateInfo("1", "2", [Wheel("yt-dlp", "2", "https://f/a.whl", sha, "yt_dlp-2-py3-none-any.whl")])
    with pytest.raises(ValueError, match="does not contain"):
        updater.install_update(info, opener=fake_pypi({"https://f/a.whl": src.read_bytes()}),
                               engine_dir=tmp_path / "engine")


# ---- bootstrap (in a subprocess so sys.modules/sys.path of the test run stay clean) ----------------

def run_bootstrap(engine_dir: Path, packages: dict | None = None, allow_older: bool = True) -> dict:
    import_names = list((packages or updater.PACKAGES).values())
    code = textwrap.dedent(f"""
        import json, sys
        from pathlib import Path
        sys.path.insert(0, {str(ROOT)!r})
        from app import updater
        st = updater.bootstrap_engine(Path({str(engine_dir)!r}), packages={packages!r},
                                      allow_older={allow_older!r})
        mods = {{n: getattr(sys.modules.get(n), "__file__", None) for n in {import_names!r}}}
        print(json.dumps({{"version": st.version, "source": st.source, "error": st.error, "mods": mods}}))
    """)
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=ROOT,
                         env={**os.environ, "VD_APPDATA": str(engine_dir.parent)})
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_bootstrap_without_download_uses_bundled(tmp_path):
    res = run_bootstrap(tmp_path / "engine", packages=None, allow_older=False)
    assert res["source"] == "bundled"
    import yt_dlp
    assert version_tuple(res["version"]) == version_tuple(yt_dlp.version.__version__)


def test_bootstrap_loads_from_wheel(tmp_path):
    engine_dir = tmp_path / "engine"
    engine_dir.mkdir()
    sha_a = make_wheel(engine_dir / "a.whl", "vdfake", "__version__ = '9.9'\n")
    sha_b = make_wheel(engine_dir / "b.whl", "vdfake_ejs", "__version__ = '1.0'\n")
    write_manifest(engine_dir, {
        "vdfake": {"version": "9.9", "file": "a.whl", "sha256": sha_a},
        "vdfake-ejs": {"version": "1.0", "file": "b.whl", "sha256": sha_b},
    })
    res = run_bootstrap(engine_dir, packages={"vdfake": "vdfake", "vdfake-ejs": "vdfake_ejs"})
    assert res["source"] == "updated", res
    assert res["version"] == "9.9"
    assert res["mods"]["vdfake"].startswith(str(engine_dir / "a.whl"))


def test_bootstrap_ignores_tampered_wheel(tmp_path):
    engine_dir = tmp_path / "engine"
    engine_dir.mkdir()
    make_wheel(engine_dir / "a.whl", "yt_dlp", "raise SystemExit('should never run')\n")
    write_manifest(engine_dir, {"yt-dlp": {"version": "9999.1.1", "file": "a.whl", "sha256": "0" * 64}})
    res = run_bootstrap(engine_dir, packages={"yt-dlp": "yt_dlp"})
    assert res["source"] == "bundled"
    assert "integrity" in res["error"]
    assert "site-packages" in res["mods"]["yt_dlp"] or "yt_dlp" in res["mods"]["yt_dlp"]


def test_bootstrap_broken_wheel_falls_back_and_is_remembered(tmp_path):
    engine_dir = tmp_path / "engine"
    engine_dir.mkdir()
    sha = make_wheel(engine_dir / "a.whl", "yt_dlp", "raise ImportError('broken engine')\n")
    write_manifest(engine_dir, {"yt-dlp": {"version": "9999.1.1", "file": "a.whl", "sha256": sha}})
    res = run_bootstrap(engine_dir, packages={"yt-dlp": "yt_dlp"})
    assert res["source"] == "bundled"
    assert "broken engine" in res["error"]
    assert not res["mods"]["yt_dlp"].startswith(str(engine_dir))  # the real, bundled yt_dlp
    manifest = updater.read_manifest(engine_dir)
    assert manifest["failed"] is True and manifest["failed_version"] == "9999.1.1"
    # A second launch doesn't even try.
    assert run_bootstrap(engine_dir, packages={"yt-dlp": "yt_dlp"})["error"] is None


def test_bootstrap_skips_download_older_than_bundled(tmp_path):
    engine_dir = tmp_path / "engine"
    engine_dir.mkdir()
    sha = make_wheel(engine_dir / "a.whl", "yt_dlp", "raise SystemExit('should never run')\n")
    write_manifest(engine_dir, {"yt-dlp": {"version": "2000.1.1", "file": "a.whl", "sha256": sha}})
    res = run_bootstrap(engine_dir, packages={"yt-dlp": "yt_dlp"}, allow_older=False)
    assert res["source"] == "bundled" and res["error"] is None


@network
def test_real_update_from_pypi_and_bootstrap(tmp_path):
    """Download the real latest yt-dlp + yt-dlp-ejs wheels and import yt_dlp from them."""
    engine_dir = tmp_path / "engine"
    info = updater.check_for_update("2000.1.1", engine_dir=engine_dir)
    assert info.available and len(info.wheels) == 2
    updater.install_update(info, engine_dir=engine_dir)
    res = run_bootstrap(engine_dir, packages=None, allow_older=True)
    assert res["source"] == "updated", res
    assert res["mods"]["yt_dlp"].startswith(str(engine_dir))
    assert res["mods"]["yt_dlp_ejs"].startswith(str(engine_dir))
    assert version_tuple(res["version"]) == version_tuple(info.latest)
