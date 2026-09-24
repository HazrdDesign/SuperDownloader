"""Quality list builder: options come from the formats the source really has."""

from app.engine import Quality, default_quality, list_qualities


def fmt(fid, height=None, vcodec="none", acodec="none", ext="mp4", size=None, width=None, tbr=None, **kw):
    d = {"format_id": fid, "url": f"https://example.invalid/{fid}", "ext": ext, "vcodec": vcodec,
         "acodec": acodec, "protocol": "https"}
    if height:
        d["height"] = height
        d["width"] = width or int(height * 16 / 9)
    if size:
        d["filesize"] = size
    if tbr:
        d["tbr"] = tbr
    d.update(kw)
    return d


def youtube_like():
    return {
        "id": "abc", "title": "Demo", "extractor": "youtube", "extractor_key": "Youtube",
        "webpage_url": "https://www.youtube.com/watch?v=abc", "duration": 100,
        "formats": [
            fmt("140", acodec="mp4a.40.2", ext="m4a", size=1_600_000),
            fmt("251", acodec="opus", ext="webm", size=1_700_000),
            fmt("160", 144, "avc1.4d400c", size=1_000_000),
            fmt("134", 360, "avc1.4d401e", size=5_000_000),
            fmt("243", 360, "vp9", ext="webm", size=4_000_000),
            fmt("136", 720, "avc1.4d401f", size=20_000_000),
            fmt("247", 720, "vp9", ext="webm", size=15_000_000),
            fmt("137", 1080, "avc1.640028", size=40_000_000),
            fmt("248", 1080, "vp9", ext="webm", size=30_000_000),
            fmt("271", 1440, "vp9", ext="webm", size=90_000_000),
            fmt("313", 2160, "vp9", ext="webm", size=200_000_000),
            fmt("401", 2160, "av01.0.12M.08", size=180_000_000),
            fmt("18", 360, "avc1.42001E", acodec="mp4a.40.2", size=6_000_000),
        ],
    }


def keys(qs):
    return [q.key for q in qs]


def test_options_follow_real_heights_deduplicated():
    qs = list_qualities(youtube_like())
    assert keys(qs) == ["best", "h2160", "h1440", "h1080", "h720", "h360", "h144", "audio"]
    assert qs[0].label == "Best available (MP4) — 2160p"
    assert qs[1].label == "2160p (4K)"
    assert qs[-1].label == "Audio only (MP3)"
    assert qs[-1].audio_only


def test_sizes_match_selected_formats_prefer_h264():
    qs = {q.key: q for q in list_qualities(youtube_like())}
    # 1080p picks H.264 (137) + M4A (140), not VP9.
    assert qs["h1080"].size == 40_000_000 + 1_600_000
    assert qs["h720"].size == 20_000_000 + 1_600_000
    # Audio: MP3 at 192 kbps for 100 s.
    assert qs["audio"].size == 100 * 192_000 // 8
    assert "~" in qs["h1080"].display and "MB" in qs["h1080"].display


def test_only_offers_heights_that_exist():
    info = youtube_like()
    info["formats"] = [f for f in info["formats"] if f.get("height") in (None, 720)]
    assert keys(list_qualities(info)) == ["best", "h720", "audio"]


def test_portrait_video_uses_short_side():
    info = {"id": "s", "title": "Short", "duration": 10, "formats": [
        fmt("v", 1920, "avc1", width=1080, size=5_000_000),
        fmt("a", acodec="mp4a.40.2", ext="m4a", size=100_000),
    ]}
    assert keys(list_qualities(info)) == ["best", "h1080", "audio"]


def test_audio_only_source_offers_only_audio():
    info = {"id": "song", "title": "Song", "duration": 60, "formats": [
        fmt("mp3", acodec="mp3", ext="mp3", size=1_000_000),
        fmt("opus", acodec="opus", ext="webm", size=900_000),
    ]}
    assert keys(list_qualities(info)) == ["audio"]


def test_single_url_without_formats_list():
    info = {"id": "x", "title": "Direct", "url": "https://example.invalid/x.mp4", "ext": "mp4",
            "height": 480, "width": 854, "vcodec": "avc1", "acodec": "mp4a"}
    assert keys(list_qualities(info)) == ["best", "h480", "audio"]


def test_unknown_codec_with_dimensions_is_video():
    info = {"id": "x", "title": "x", "formats": [
        {"format_id": "0", "url": "https://example.invalid/a.mp4", "ext": "mp4", "height": 720, "width": 1280}]}
    assert keys(list_qualities(info)) == ["best", "h720", "audio"]


def test_drm_formats_are_ignored():
    info = youtube_like()
    for f in info["formats"]:
        if f.get("height") == 2160:
            f["has_drm"] = True
    assert "h2160" not in keys(list_qualities(info))


def test_size_unknown_is_none_not_crash():
    info = {"id": "x", "title": "x", "formats": [fmt("v", 720, "avc1", acodec="mp4a")]}
    qs = list_qualities(info)
    assert qs[0].size is None
    assert qs[0].display == qs[0].label


def test_size_from_bitrate_when_no_filesize():
    info = {"id": "x", "title": "x", "duration": 80, "formats": [fmt("v", 720, "avc1", acodec="mp4a", tbr=1000)]}
    best = list_qualities(info)[0]
    assert best.size == 1000 * 1000 // 8 * 80


def test_default_quality_uses_setting_when_available():
    qs = list_qualities(youtube_like())
    assert default_quality(qs, "1080").key == "h1080"
    assert default_quality(qs, "720").key == "h720"
    assert default_quality(qs, "audio").key == "audio"
    assert default_quality(qs, "best").key == "best"


def test_default_quality_falls_back_to_best():
    info = youtube_like()
    info["formats"] = [f for f in info["formats"] if f.get("height") in (None, 360)]
    qs = list_qualities(info)
    assert default_quality(qs, "1080").key == "best"
    assert default_quality([], "best") is None


def test_ydl_opts():
    v = Quality("h1080", "1080p", height=1080).ydl_opts()
    assert v["format_sort"][0] == "res:1080"
    assert "vcodec:h264" in v["format_sort"]
    assert v["merge_output_format"] == "mp4"
    a = Quality("audio", "Audio only (MP3)", audio_only=True).ydl_opts()
    assert a["postprocessors"][0]["key"] == "FFmpegExtractAudio"
    assert a["postprocessors"][0]["preferredcodec"] == "mp3"
    assert Quality("audio", "", audio_only=True).ext == "mp3"
    assert Quality("best", "").ext == "mp4"
    assert Quality("best", "").ydl_opts()["format_sort"][0] == "res"
