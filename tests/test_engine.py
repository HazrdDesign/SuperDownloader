import json
import subprocess
import threading
from pathlib import Path

import pytest

from app import engine
from app.engine import (
    FORMATS_BY_KEY,
    CheckRequest,
    DownloadJob,
    Engine,
    Quality,
    _ProgressState,
    human_duration,
    human_size,
    normalize_url,
    unique_stem,
)
from app.errors import Status
from tests.conftest import ffmpeg_bin, requires_ffmpeg

# ---- pure helpers -----------------------------------------------------------------------------


@pytest.mark.parametrize("text,expected", [
    ("https://www.youtube.com/watch?v=abc", "https://www.youtube.com/watch?v=abc"),
    ("  https://vimeo.com/123  ", "https://vimeo.com/123"),
    ("vimeo.com/123", "https://vimeo.com/123"),
    ("www.youtube.com/watch?v=x", "https://www.youtube.com/watch?v=x"),
    ("<https://example.com/v>", "https://example.com/v"),
    ("http://127.0.0.1:8000/a.mp4", "http://127.0.0.1:8000/a.mp4"),
])
def test_normalize_url_accepts(text, expected):
    assert normalize_url(text) == expected


@pytest.mark.parametrize("text", [
    "", "   ", "hello world", "asdfghjkl", "not a url at all", "ftp://example.com/x", "javascript:alert(1)",
    "file:///C:/x.mp4", "https://", "http://nodot/path",
])
def test_normalize_url_rejects(text):
    assert normalize_url(text) is None


def test_invalid_url_check_is_instant_and_red():
    res = Engine().check_link(CheckRequest("this is garbage"))
    assert res.result.status is Status.INVALID_URL
    assert not res.ok


def test_unique_stem(tmp_path):
    assert unique_stem(tmp_path, "Video", "mp4") == "Video"
    (tmp_path / "Video.mp4").write_bytes(b"x")
    assert unique_stem(tmp_path, "Video", "mp4") == "Video (1)"
    (tmp_path / "Video (1).mp4").write_bytes(b"x")
    assert unique_stem(tmp_path, "Video", "mp4") == "Video (2)"
    # The MP3 of a video whose MP4 exists keeps the plain name.
    assert unique_stem(tmp_path, "Video", "mp3") == "Video"
    (tmp_path / "Video.mp3").write_bytes(b"x")
    assert unique_stem(tmp_path, "Video", "mp3") == "Video (1)"
    # Anything that could collide with yt-dlp's intermediate files blocks the name.
    (tmp_path / "Other.f137.mp4.part").write_bytes(b"x")
    assert unique_stem(tmp_path, "Other", "mp4") == "Other (1)"
    (tmp_path / "Song.webm").write_bytes(b"x")
    assert unique_stem(tmp_path, "Song", "mp3") == "Song (1)"


def test_unique_stem_case_insensitive(tmp_path):
    (tmp_path / "VIDEO.MP4").write_bytes(b"x")
    assert unique_stem(tmp_path, "video", "mp4") == "video (1)"


def test_human_formatting():
    assert human_size(None) == "?"
    assert human_size(52 * 1024 * 1024) == "52 MB"
    assert human_size(3.5 * 1024 ** 3) == "3.5 GB"
    assert human_duration(65) == "1:05"
    assert human_duration(3725) == "1:02:05"
    assert human_duration(None) == ""


def test_progress_combines_video_and_audio_parts():
    parts = [{"format_id": "137", "filesize": 90}, {"format_id": "140", "filesize": 10}]
    st = _ProgressState()
    p = st.update({"status": "downloading", "filename": "a.f137.mp4", "downloaded_bytes": 45,
                   "total_bytes": 90, "speed": 10, "eta": 4,
                   "info_dict": {"requested_formats": parts, "format_id": "137"}})
    assert p.percent == pytest.approx(45)
    assert p.eta == pytest.approx((45 + 10) / 10)
    st.update({"status": "finished", "filename": "a.f137.mp4", "total_bytes": 90,
               "info_dict": {"requested_formats": parts, "format_id": "137"}})
    p = st.update({"status": "downloading", "filename": "a.f140.m4a", "downloaded_bytes": 5,
                   "total_bytes": 10, "info_dict": {"requested_formats": parts, "format_id": "140"}})
    assert p.percent == pytest.approx(95)


def test_progress_single_file_and_fragments():
    st = _ProgressState(index=2, items=3, title="T")
    p = st.update({"status": "downloading", "downloaded_bytes": 1, "total_bytes_estimate": 4, "info_dict": {}})
    assert p.percent == pytest.approx(25) and p.item == 2 and p.items == 3
    p = st.update({"status": "downloading", "downloaded_bytes": 10, "fragment_index": 3,
                   "fragment_count": 12, "info_dict": {}})
    assert p.percent == pytest.approx(25)
    p = st.update({"status": "downloading", "downloaded_bytes": 10, "info_dict": {}})
    assert p.percent is None
    assert st.processing().phase == "processing"


def test_download_result_error_states():
    r = engine.DownloadResult(cancelled=True)
    assert r.error.status is Status.CANCELLED
    assert engine.DownloadResult(files=[Path("x")]).error is None


def test_password_is_redacted():
    e = Engine()
    e._base_opts(engine.NONE_SOURCE, "hunter2", None, engine._YdlLogger(lambda s: s))
    assert "hunter2" not in e.redact("sending videopassword=hunter2")
    assert e.redact("Cookie: SID=abc; HSID=def") == "Cookie: <redacted>"


def test_base_opts_never_allow_drm_and_pass_login(tmp_path):
    from app.browsers import LoginSource
    e = Engine()
    src = LoginSource("firefox:/p", "Zen — Default", "Zen", "firefox", "/p")
    opts = e._base_opts(src, "pw", "https://site.example/page", engine._YdlLogger(lambda s: s))
    assert opts["allow_unplayable_formats"] is False
    assert opts["cookiesfrombrowser"] == ("firefox", "/p", None, None)
    assert opts["videopassword"] == "pw"
    assert opts["http_headers"] == {"Referer": "https://site.example/page"}
    assert opts["noplaylist"] is True
    plain = e._base_opts(engine.NONE_SOURCE, None, None, engine._YdlLogger(lambda s: s))
    assert "cookiesfrombrowser" not in plain and "videopassword" not in plain and "http_headers" not in plain


def test_check_classifies_cookie_failure_without_traceback(tmp_path):
    """A login source pointing at a folder with a broken cookie DB gives a plain state, not a crash."""
    from app.browsers import LoginSource
    prof = tmp_path / "prof"
    prof.mkdir()
    (prof / "cookies.sqlite").write_bytes(b"this is not sqlite")
    src = LoginSource(f"custom:{prof}", "Custom", "your browser", "firefox", str(prof))
    res = Engine().check_link(CheckRequest("http://127.0.0.1:9/video.mp4", login=src))
    assert not res.ok
    assert "Traceback" not in res.result.message


# ---- local end-to-end (real yt-dlp + ffmpeg against a local HTTP server) -----------------------


def ffprobe_json(path: Path) -> dict:
    out = subprocess.run([ffmpeg_bin("ffprobe"), "-v", "error", "-show_streams", "-show_format",
                          "-of", "json", str(path)], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


@requires_ffmpeg
def test_check_direct_video_is_ready(media_server):
    res = Engine().check_link(CheckRequest(f"{media_server}/clip.mp4"))
    assert res.ok, res.result
    # A raw file link has no height metadata, so only Best + Audio are offered.
    assert [q.key for q in res.qualities] == ["best"]
    assert res.video.title
    assert res.playlist is None


@requires_ffmpeg
def test_check_page_without_video_is_unsupported(media_server):
    res = Engine().check_link(CheckRequest(f"{media_server}/novideo.html"))
    assert res.result.status is Status.UNSUPPORTED


@requires_ffmpeg
def test_check_missing_file_is_removed(media_server):
    res = Engine().check_link(CheckRequest(f"{media_server}/gone.mp4"))
    assert res.result.status is Status.REMOVED


@requires_ffmpeg
def test_check_unreachable_is_network():
    res = Engine().check_link(CheckRequest("http://127.0.0.1:9/video.mp4"))
    assert res.result.status is Status.NETWORK


@requires_ffmpeg
def test_check_page_with_two_videos_is_playlist(media_server):
    res = Engine().check_link(CheckRequest(f"{media_server}/page.html"))
    assert res.ok, res.result
    assert res.playlist is not None
    assert res.playlist.count == 2
    assert res.playlist.pure


@requires_ffmpeg
def test_download_mp4_then_duplicate_gets_suffix(media_server, tmp_path):
    e = Engine()
    events = []
    job = DownloadJob([f"{media_server}/clip.mp4"], Quality("best", "Best"), tmp_path)
    r1 = e.download(job, events.append)
    assert r1.error is None, r1.failures
    [f1] = r1.files
    assert f1.suffix == ".mp4" and f1.is_file()
    assert any(ev.phase == "downloading" for ev in events)
    r2 = e.download(job, events.append)
    [f2] = r2.files
    assert f2.name == f1.stem + " (1).mp4"
    assert f1.read_bytes() == f2.read_bytes()  # the first file was not overwritten
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted([f1.name, f2.name])


@requires_ffmpeg
def test_download_audio_only_is_playable_mp3(media_server, tmp_path):
    job = DownloadJob([f"{media_server}/clip.mp4"], None, tmp_path, fmt=FORMATS_BY_KEY["mp3"])
    r = Engine().download(job, lambda p: None)
    assert r.error is None, r.failures
    [mp3] = r.files
    assert mp3.suffix == ".mp3"
    probe = ffprobe_json(mp3)
    assert [s["codec_name"] for s in probe["streams"]] == ["mp3"]
    assert float(probe["format"]["duration"]) > 5
    assert list(tmp_path.iterdir()) == [mp3]  # the intermediate audio file was removed


@requires_ffmpeg
def test_cancel_mid_download_leaves_no_partial_files(media_server, tmp_path):
    (tmp_path / "keep-me.txt").write_text("pre-existing")
    e = Engine()
    started = threading.Event()

    def on_progress(p):
        if p.phase == "downloading" and (p.downloaded or 0) > 200_000:
            started.set()
            e.cancel()

    job = DownloadJob([f"{media_server}/big.mp4"], Quality("best", "Best"), tmp_path)
    r = e.download(job, on_progress)
    assert started.is_set(), "download never reached the cancel point"
    assert r.cancelled
    assert r.error.status is Status.CANCELLED
    assert [p.name for p in tmp_path.iterdir()] == ["keep-me.txt"]


@requires_ffmpeg
def test_download_playlist_all(media_server, tmp_path):
    e = Engine()
    res = e.check_link(CheckRequest(f"{media_server}/page.html"))
    job = DownloadJob(res.playlist.entry_urls, res.qualities[0], tmp_path)
    seen_items = set()
    r = e.download(job, lambda p: seen_items.add((p.item, p.items)))
    assert r.error is None, r.failures
    assert len(r.files) == 2
    assert {(1, 2), (2, 2)} <= seen_items


@requires_ffmpeg
def test_download_failure_cleans_up_and_reports(media_server, tmp_path):
    job = DownloadJob([f"{media_server}/gone.mp4"], Quality("best", "Best"), tmp_path)
    r = Engine().download(job, lambda p: None)
    assert r.files == []
    assert r.error.status is Status.REMOVED
    assert list(tmp_path.iterdir()) == []


@requires_ffmpeg
def test_dash_separate_streams_offer_real_heights(media_server):
    res = Engine().check_link(CheckRequest(f"{media_server}/dash/manifest.mpd"))
    assert res.ok, res.result
    assert [q.key for q in res.qualities] == ["best", "h720", "h360"]


def dash_listing(media_dir: Path) -> str:
    d = media_dir / "dash"
    return f"files={sorted(p.name for p in d.iterdir())}\nmanifest={(d / 'manifest.mpd').read_text()[:2000]}"


@requires_ffmpeg
@pytest.mark.parametrize("key,height", [("h720", 720), ("h360", 360)])
def test_dash_download_merges_to_mp4_at_chosen_height(media_server, media_dir, tmp_path, key, height):
    e = Engine()
    res = e.check_link(CheckRequest(f"{media_server}/dash/manifest.mpd"))
    quality = next(q for q in res.qualities if q.key == key)
    percents = []
    r = e.download(DownloadJob([res.url], quality, tmp_path),
                   lambda p: percents.append(p.percent) if p.percent is not None else None)
    assert r.error is None, (r.failures, dash_listing(media_dir))
    [f] = r.files
    assert f.suffix == ".mp4"
    probe = ffprobe_json(f)
    codecs = sorted(s["codec_name"] for s in probe["streams"])
    assert codecs == ["aac", "h264"]
    video = next(s for s in probe["streams"] if s["codec_type"] == "video")
    assert video["height"] == height
    assert list(tmp_path.iterdir()) == [f]  # the separate video/audio parts were merged and removed
    assert percents and max(percents) <= 100.0
    assert percents == sorted(percents) or max(percents) > 90  # overall progress, not per-part resets


@requires_ffmpeg
def test_cancel_during_fragment_download_leaves_no_partial_files(media_server, media_dir, tmp_path):
    e = Engine()
    res = e.check_link(CheckRequest(f"{media_server}/dashslow/manifest.mpd"))
    assert res.ok, res.result
    hit = threading.Event()

    def on_progress(p):
        if p.phase == "downloading" and (p.downloaded or 0) > 0:
            hit.set()
            e.cancel()

    r = e.download(DownloadJob([res.url], res.qualities[0], tmp_path), on_progress)
    assert hit.is_set(), (r.failures, dash_listing(media_dir))
    assert r.cancelled
    assert list(tmp_path.iterdir()) == []


def test_progress_fragment_estimate_is_clamped_and_never_goes_back():
    st = _ProgressState()
    frag = {"status": "downloading", "fragment_count": 10, "info_dict": {}}
    # yt-dlp's early byte estimate says 50%, but we're still in fragment 0 of 10.
    assert st.update({**frag, "fragment_index": 0, "downloaded_bytes": 5, "total_bytes_estimate": 10}).percent \
        == pytest.approx(10)
    # Estimate says 20% but 5 of 10 fragments are done.
    assert st.update({**frag, "fragment_index": 5, "downloaded_bytes": 2, "total_bytes_estimate": 10}).percent \
        == pytest.approx(50)
    # A noisy, lower reading never moves the bar backwards.
    assert st.update({**frag, "fragment_index": 4, "downloaded_bytes": 1, "total_bytes_estimate": 10}).percent \
        == pytest.approx(50)


def test_safe_folder_name():
    from app.engine import safe_folder_name
    assert safe_folder_name('My: "Mix"?') not in ("", "Playlist")
    assert not any(c in safe_folder_name('a<b>c|d*e/f\\g') for c in '<>|*/\\')
    assert safe_folder_name("...") == "Playlist"
    assert safe_folder_name("") == "Playlist"
    assert len(safe_folder_name("x" * 500)) <= 100


# ---- formats, clips, subtitles, projects ------------------------------------------------------

def test_timecode_parsing_and_suffix():
    from app.engine import clip_suffix, format_timecode, parse_timecode
    assert parse_timecode("83") == 83
    assert parse_timecode("1:23") == 83
    assert parse_timecode("0:01:23.5") == 83.5
    assert parse_timecode("1:02:03") == 3723
    assert parse_timecode("") is None and parse_timecode("abc") is None
    assert format_timecode(83.5) == "1:23.5"
    assert clip_suffix((42, 58)) == " (clip 0m42s-0m58s)"
    assert clip_suffix(None) == ""


def test_project_prefix_and_folders():
    from app.engine import project_parts, project_prefix
    assert project_parts("Nike / Spring 2027") == ["Nike", "Spring 2027"]
    assert project_parts("  ") == []
    assert project_prefix("Nike / Spring 2027", "2026-09-25") == "Nike_Spring-2027_2026-09-25_"
    assert project_prefix("", "2026-09-25") == ""
    assert not any(c in "".join(project_parts('a:b / c?d')) for c in ':?')


def test_extract_urls_for_batch_paste():
    from app.engine import extract_urls
    text = "refs:\nhttps://vimeo.com/1\nhttps://www.youtube.com/watch?v=abc, vimeo.com/2 (dup) https://vimeo.com/1."
    assert extract_urls(text) == ["https://vimeo.com/1", "https://www.youtube.com/watch?v=abc"]
    assert extract_urls("nothing here") == []


def test_frame_rate_argument():
    from app.engine import _frame_rate_arg
    assert _frame_rate_arg(29.97) == "30000/1001"
    assert _frame_rate_arg(23.976) == "24000/1001"
    assert _frame_rate_arg(25) == "25"
    assert _frame_rate_arg(None) is None


def test_unique_stem_across_new_formats(tmp_path):
    (tmp_path / "Clip.mp4").write_bytes(b"x")
    assert unique_stem(tmp_path, "Clip", "mov") == "Clip"
    assert unique_stem(tmp_path, "Clip", "wav") == "Clip"
    (tmp_path / "Clip.mov").write_bytes(b"x")
    assert unique_stem(tmp_path, "Clip", "mov") == "Clip (1)"


def test_login_fallback_retry(monkeypatch):
    """A 'needs login' result is retried once with the browser login, and says so."""
    from app.browsers import LoginSource
    from app.engine import CheckResult
    from app.errors import classify_error, ready
    zen = LoginSource("firefox:/z", "Zen — Default", "Zen", "firefox", "/z")
    calls = []

    def fake_once(self, req):
        calls.append(req.login)
        if req.login.is_none:
            return CheckResult(classify_error("The web client only works when logged-in"))
        return CheckResult(ready(), url=req.url)

    monkeypatch.setattr(Engine, "_check_once", fake_once)
    res = Engine().check_link(CheckRequest("https://vimeo.com/1", fallback_login=zen))
    assert res.ok and res.used_login
    assert calls == [engine.NONE_SOURCE, zen]
    # No fallback: the needs-login result is returned as is.
    calls.clear()
    res = Engine().check_link(CheckRequest("https://vimeo.com/1"))
    assert res.result.status is Status.NEEDS_LOGIN and not res.used_login and len(calls) == 1


@requires_ffmpeg
@pytest.mark.parametrize("key,codec,ext", [("edit", "h264", ".mp4"), ("prores", "prores", ".mov")])
def test_edit_ready_and_prores_conversion(media_server, tmp_path, key, codec, ext):
    e = Engine()
    res = e.check_link(CheckRequest(f"{media_server}/dash/manifest.mpd"))
    phases = []
    r = e.download(DownloadJob([res.url], res.qualities[-1], tmp_path, fmt=FORMATS_BY_KEY[key]),
                   lambda p: phases.append((p.phase, p.percent)))
    assert r.error is None, r.failures
    [f] = r.files
    assert f.suffix == ext
    probe = ffprobe_json(f)
    video = next(s for s in probe["streams"] if s["codec_type"] == "video")
    audio = next(s for s in probe["streams"] if s["codec_type"] == "audio")
    assert video["codec_name"] == codec
    assert video["r_frame_rate"] == video["avg_frame_rate"]  # constant frame rate
    assert audio["codec_name"] == ("aac" if key == "edit" else "pcm_s16le")
    assert any(ph == "converting" and pct == 100.0 for ph, pct in phases)
    assert list(tmp_path.iterdir()) == [f]  # the downloaded source was removed


@requires_ffmpeg
def test_wav_audio(media_server, tmp_path):
    r = Engine().download(DownloadJob([f"{media_server}/clip.mp4"], None, tmp_path, fmt=FORMATS_BY_KEY["wav"]),
                          lambda p: None)
    assert r.error is None, r.failures
    [f] = r.files
    assert f.suffix == ".wav"
    assert [s["codec_name"] for s in ffprobe_json(f)["streams"]][0].startswith("pcm_")


@requires_ffmpeg
def test_clip_range_downloads_only_that_part(media_server, tmp_path):
    e = Engine()
    res = e.check_link(CheckRequest(f"{media_server}/clip.mp4"))
    r = e.download(DownloadJob([res.url], res.qualities[0], tmp_path, clip=(1.0, 3.0)), lambda p: None)
    assert r.error is None, r.failures
    [f] = r.files
    assert f.name.endswith(" (clip 0m01s-0m03s).mp4")
    duration = float(ffprobe_json(f)["format"]["duration"])
    assert 1.5 < duration < 2.6


@requires_ffmpeg
def test_subtitles_listed_and_saved_as_srt(media_server, tmp_path):
    e = Engine()
    res = e.check_link(CheckRequest(f"{media_server}/withsubs.html"))
    assert res.ok, res.result
    assert [s.lang for s in res.subtitles] == ["en"]
    assert res.subtitles[0].label == "English"
    r = e.download(DownloadJob([res.url], res.qualities[0], tmp_path, subtitles=res.subtitles[0]), lambda p: None)
    assert r.error is None, r.failures
    [video] = r.files
    srt = tmp_path / f"{video.stem}.en.srt"
    assert srt.is_file(), sorted(p.name for p in tmp_path.iterdir())
    assert "Hello there" in srt.read_text(encoding="utf-8")


@requires_ffmpeg
def test_thumbnail_jpg(media_server, tmp_path):
    e = Engine()
    res = e.check_link(CheckRequest(f"{media_server}/withsubs.html"))
    r = e.download(DownloadJob([res.url], None, tmp_path, fmt=FORMATS_BY_KEY["jpg"]), lambda p: None)
    assert r.error is None, r.failures
    [f] = r.files
    assert f.suffix == ".jpg"
    from PIL import Image
    with Image.open(f) as img:
        assert img.format == "JPEG" and img.size == (1280, 720)


@requires_ffmpeg
def test_project_prefix_in_file_name(media_server, tmp_path):
    e = Engine()
    res = e.check_link(CheckRequest(f"{media_server}/clip.mp4"))
    r = e.download(DownloadJob([res.url], res.qualities[0], tmp_path, name_prefix="Nike_Spring_2026-09-25_"),
                   lambda p: None)
    [f] = r.files
    assert f.name.startswith("Nike_Spring_2026-09-25_")


@requires_ffmpeg
def test_cancel_during_conversion_leaves_nothing(media_server, tmp_path):
    e = Engine()
    res = e.check_link(CheckRequest(f"{media_server}/dash/manifest.mpd"))

    def on_progress(p):
        if p.phase == "converting":
            e.cancel()

    r = e.download(DownloadJob([res.url], res.qualities[0], tmp_path, fmt=FORMATS_BY_KEY["prores"]), on_progress)
    assert r.cancelled
    assert list(tmp_path.iterdir()) == []


def test_bundled_ffmpeg_is_put_on_path(tmp_path, monkeypatch):
    """yt-dlp's clip-range check only searches PATH, so the bundled FFmpeg must be on it."""
    import os
    monkeypatch.setattr(engine.paths, "ffmpeg_dir", lambda: tmp_path)
    monkeypatch.setenv("PATH", "C:\\Windows")
    Engine()
    assert os.environ["PATH"].split(os.pathsep)[0] == str(tmp_path)
    Engine()  # only added once
    assert os.environ["PATH"].split(os.pathsep).count(str(tmp_path)) == 1
