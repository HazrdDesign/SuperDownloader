import functools
import http.server
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def _isolated_appdata(tmp_path_factory, monkeypatch):
    """Never touch the real %APPDATA% from tests."""
    if "VD_APPDATA" not in os.environ:
        monkeypatch.setenv("VD_APPDATA", str(tmp_path_factory.mktemp("appdata")))


def _ffmpeg_available() -> bool:
    from app import paths
    if paths.ffmpeg_dir():
        return True
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def ffmpeg_bin(name: str) -> str:
    from app import paths
    d = paths.ffmpeg_dir()
    if d:
        return str(d / (name + (".exe" if sys.platform == "win32" else "")))
    return shutil.which(name)


requires_ffmpeg = pytest.mark.skipif(not _ffmpeg_available(), reason="ffmpeg/ffprobe not available")


@pytest.fixture(scope="session")
def media_dir(tmp_path_factory):
    """Small real media files generated with ffmpeg."""
    if not _ffmpeg_available():
        pytest.skip("ffmpeg/ffprobe not available")
    d = tmp_path_factory.mktemp("media")
    ff = ffmpeg_bin("ffmpeg")
    common = [ff, "-hide_banner", "-loglevel", "error", "-y"]
    # 6 s, 640x360 H.264 + AAC
    subprocess.run(common + [
        "-f", "lavfi", "-i", "testsrc=size=640x360:rate=25:duration=6",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
        str(d / "clip.mp4")], check=True)
    # A bigger file (~4 MB of noise) used for the cancel test; served slowly.
    subprocess.run(common + [
        "-f", "lavfi", "-i", "testsrc=size=640x360:rate=25:duration=10",
        "-f", "lavfi", "-i", "anoisesrc=duration=10",
        "-c:v", "libx264", "-preset", "ultrafast", "-qp", "5", "-pix_fmt", "yuv420p", "-c:a", "aac",
        "-shortest", str(d / "big.mp4")], check=True)
    # DASH: separate 720p/360p H.264 video tracks and an AAC audio track, 2 s segments,
    # like YouTube/Vimeo serve them. Exercises merging and fragment downloads.
    dash = d / "dash"
    dash.mkdir()
    subprocess.run(common + [
        "-f", "lavfi", "-i", "testsrc=size=1280x720:rate=25:duration=12",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=12",
        "-map", "0:v", "-map", "0:v", "-map", "1:a",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-g", "50", "-qp", "10",
        "-s:v:0", "1280x720", "-s:v:1", "640x360", "-c:a", "aac",
        "-seg_duration", "2", "-adaptation_sets", "id=0,streams=v id=1,streams=a",
        "-use_template", "1", "-use_timeline", "1",
        "-init_seg_name", "init-$RepresentationID$.m4s",
        "-media_seg_name", "chunk-$RepresentationID$-$Number%05d$.m4s",
        "manifest.mpd"], check=True, cwd=dash)
    (d / "page.html").write_text(
        "<html><head><title>Two clips</title></head><body>"
        "<video src='clip.mp4'></video><video src='clip2.mp4'></video></body></html>", encoding="utf-8")
    shutil.copy(d / "clip.mp4", d / "clip2.mp4")
    # A page with a poster image and English subtitles (HTML5 <track>), for JPG + subtitle tests.
    subprocess.run(common + ["-f", "lavfi", "-i", "testsrc=size=1280x720:duration=1", "-frames:v", "1",
                             str(d / "poster.png")], check=True)
    (d / "subs.vtt").write_text(
        "WEBVTT\n\n00:00:00.000 --> 00:00:02.000\nHello there\n\n00:00:02.000 --> 00:00:05.000\n"
        "Second line\n", encoding="utf-8")
    (d / "withsubs.html").write_text(
        "<html><head><title>Clip with subtitles</title>"
        "<meta property='og:image' content='poster.png'></head><body>"
        "<video src='clip.mp4' poster='poster.png'><track kind='subtitles' src='subs.vtt' srclang='en' "
        "label='English'></video></body></html>", encoding="utf-8")
    (d / "novideo.html").write_text(
        "<html><head><title>Just text</title></head><body><p>Hello, no video here.</p></body></html>",
        encoding="utf-8")
    return d


class _Handler(http.server.SimpleHTTPRequestHandler):
    """Serves the media folder. ``big.mp4`` and everything under ``/dashslow/`` (a mirror of
    ``/dash/``) are sent slowly so cancel tests can interrupt them."""

    slow_names = ("big.mp4",)

    def log_message(self, *args):
        pass

    def translate_path(self, path):
        return super().translate_path(path.replace("/dashslow/", "/dash/", 1))

    def send_head(self):
        """Adds HTTP Range support (FFmpeg seeks with it, e.g. for clip downloads)."""
        rng = self.headers.get("Range", "")
        path = self.translate_path(self.path)
        if not rng.startswith("bytes=") or os.path.isdir(path) or not os.path.isfile(path):
            return super().send_head()
        size = os.path.getsize(path)
        start_s, _, end_s = rng[6:].split(",")[0].partition("-")
        if start_s:
            start = int(start_s)
            end = min(int(end_s), size - 1) if end_s else size - 1
        else:  # suffix range: last N bytes
            start, end = max(0, size - int(end_s)), size - 1
        if start >= size:
            self.send_error(416)
            return None
        f = open(path, "rb")
        f.seek(start)
        self.send_response(206)
        self.send_header("Content-Type", self.guess_type(path))
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        self._range_left = end - start + 1
        return f

    def copyfile(self, source, outputfile):
        left = getattr(self, "_range_left", None)
        if left is None:
            return self._copy(source, outputfile)
        self._range_left = None
        while left > 0 and (chunk := source.read(min(64 * 1024, left))):
            outputfile.write(chunk)
            left -= len(chunk)

    def _copy(self, source, outputfile):
        if self.path.startswith("/dashslow/") or any(self.path.endswith(n) for n in self.slow_names):
            while chunk := source.read(64 * 1024):
                outputfile.write(chunk)
                time.sleep(0.05)  # ~1.3 MB/s so the cancel test can interrupt it
        else:
            shutil.copyfileobj(source, outputfile)


@pytest.fixture(scope="session")
def media_server(media_dir):
    handler = functools.partial(_Handler, directory=str(media_dir))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
