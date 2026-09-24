"""Automated part of the acceptance tests, against real sites.

Runs the same engine the app uses (bundled FFmpeg/Deno from vendor\\) and writes a
JSON + Markdown report. Covers: public YouTube and Vimeo (check, real quality list,
1080p MP4 download), audio-only MP3, invalid URL, non-video page, cancel cleanup and
the "(1)" duplicate name. The private-video/login test (acceptance 4) needs a real
browser profile and is manual; see docs/*-acceptance-checklist.md.

Usage:  python scripts/acceptance_smoke.py [output_dir]
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import paths  # noqa: E402
from app.engine import CheckRequest, DownloadJob, Engine  # noqa: E402

# Several public candidates per site: cloud CI IPs are often blocked or served different pages,
# so every candidate's check result is reported and the first that works is downloaded.
YOUTUBE = [
    "https://www.youtube.com/watch?v=jNQXAC9IVRw",   # "Me at the zoo" (19 s, low resolution)
    "https://www.youtube.com/watch?v=BaW_jenozKc",   # yt-dlp's own test video
    "https://www.youtube.com/watch?v=aqz-KE-bpKQ",   # Big Buck Bunny (has 1080p)
]
VIMEO = [
    "https://vimeo.com/76979871",                     # public Vimeo staff video with 1080p
    "https://player.vimeo.com/video/76979871",        # the same video's public player page
]
MAX_DOWNLOAD = 250 * 1024 * 1024                      # keep CI downloads reasonable
NOT_VIDEO = "https://example.com/"
GARBAGE = "this is not a link"

results: list[dict] = []


def record(name: str, ok: bool, **info) -> None:
    results.append({"test": name, "ok": ok, **info})
    print(("PASS " if ok else "FAIL ") + name, json.dumps(info, default=str)[:400], flush=True)


def probe(path: Path) -> dict:
    ffprobe = paths.ffmpeg_dir() / ("ffprobe.exe" if sys.platform == "win32" else "ffprobe")
    out = subprocess.run([str(ffprobe), "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
                         capture_output=True, text=True, check=True)
    data = json.loads(out.stdout)
    streams = [{"type": s["codec_type"], "codec": s["codec_name"], "height": s.get("height")} for s in data["streams"]]
    return {"streams": streams, "duration": float(data["format"].get("duration", 0)), "size": path.stat().st_size}


def pick_1080(res):
    """1080p if the video has it (and it isn't huge), else the tallest height below it."""
    heights = [q for q in res.qualities if q.height and q.height <= 1080
               and (q.size is None or q.size <= MAX_DOWNLOAD)]
    for q in heights:
        if q.height == 1080:
            return q
    return heights[0] if heights else res.qualities[0]


def site_test(label: str, urls: list[str], out: Path, engine: Engine) -> None:
    res = None
    for url in urls:
        t0 = time.time()
        candidate = engine.check_link(CheckRequest(url))
        record(f"{label}: check {url} shows Ready", candidate.ok, status=candidate.result.status.value,
               message=candidate.result.message,
               detail=candidate.result.detail[-600:] if not candidate.ok else "",
               seconds=round(time.time() - t0, 1), qualities=[q.display for q in candidate.qualities],
               title=candidate.video.title if candidate.video else None)
        if candidate.ok and res is None:
            res = candidate
    if res is None:
        return
    q = pick_1080(res)
    folder = out / label.lower()
    r = engine.download(DownloadJob([res.url], q, folder), lambda p: None)
    if r.files:
        info = probe(r.files[0])
        v = [s for s in info["streams"] if s["type"] == "video"]
        a = [s for s in info["streams"] if s["type"] == "audio"]
        ok = r.files[0].suffix == ".mp4" and bool(v) and bool(a) and (q.height is None or v[0]["height"] == q.height)
        record(f"{label}: {q.label} MP4 download", ok, file=r.files[0].name, quality=q.label, **info)
    else:
        record(f"{label}: {q.label} MP4 download", False, error=r.error.message if r.error else None,
               detail=r.error.detail[-600:] if r.error else None)
        return

    # Duplicate -> "(1)"
    r2 = engine.download(DownloadJob([res.url], q, folder), lambda p: None)
    ok = bool(r2.files) and r2.files[0].name == f"{r.files[0].stem} (1).mp4" and r.files[0].exists()
    record(f"{label}: second download is saved as '(1)'", ok,
           names=sorted(p.name for p in folder.iterdir()))

    # Audio only -> playable MP3
    audio = next(x for x in res.qualities if x.audio_only)
    r3 = engine.download(DownloadJob([res.url], audio, out / f"{label.lower()}-audio"), lambda p: None)
    if r3.files:
        info = probe(r3.files[0])
        ok = r3.files[0].suffix == ".mp3" and [s["codec"] for s in info["streams"]] == ["mp3"] and info["duration"] > 1
        record(f"{label}: audio only is a playable MP3", ok, file=r3.files[0].name, **info)
    else:
        record(f"{label}: audio only is a playable MP3", False, error=r3.error.message if r3.error else None)


def cancel_test(out: Path, engine: Engine) -> None:
    res = None
    for url in VIMEO + YOUTUBE:
        candidate = engine.check_link(CheckRequest(url))
        if candidate.ok:
            res = candidate
            break
    if res is None:
        record("Cancel mid-download leaves no partial files", False, skipped="no live video could be checked")
        return
    folder = out / "cancel"
    folder.mkdir(parents=True)
    hit = threading.Event()

    def on_progress(p):
        if p.phase == "downloading" and (p.downloaded or 0) > 500_000:
            hit.set()
            engine.cancel()

    r = engine.download(DownloadJob([res.url], res.qualities[0], folder), on_progress)
    left = sorted(p.name for p in folder.iterdir())
    record("Cancel mid-download leaves no partial files", hit.is_set() and r.cancelled and not left,
           cancelled=r.cancelled, reached_cancel_point=hit.is_set(), leftover=left)


def main() -> int:
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix="vd-acceptance-"))
    out_dir.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="vd-dl-"))
    engine = Engine()
    print(f"FFmpeg: {paths.ffmpeg_dir()}  Deno: {paths.deno_exe()}", flush=True)

    for name, fn in [
        ("YouTube", lambda: site_test("YouTube", YOUTUBE, work, engine)),
        ("Vimeo", lambda: site_test("Vimeo", VIMEO, work, engine)),
        ("Cancel", lambda: cancel_test(work, engine)),
    ]:
        try:
            fn()
        except Exception:  # noqa: BLE001
            record(f"{name}: crashed", False, traceback=traceback.format_exc())

    r = engine.check_link(CheckRequest(GARBAGE))
    record("Garbage text shows invalid URL", r.result.status.value == "invalid_url", message=r.result.message)
    r = engine.check_link(CheckRequest(NOT_VIDEO))
    record("Non-video page shows unsupported", r.result.status.value == "unsupported", status=r.result.status.value,
           message=r.result.message)

    (out_dir / "acceptance.json").write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    lines = ["| Result | Test | Notes |", "|---|---|---|"]
    for r in results:
        notes = {k: v for k, v in r.items() if k in ("status", "message", "file", "quality", "streams", "names",
                                                     "leftover", "qualities", "error", "detail")}
        lines.append(f"| {'PASS' if r['ok'] else 'FAIL'} | {r['test']} | {json.dumps(notes, default=str)[:500]} |")
    (out_dir / "acceptance.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 0 if all(r["ok"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
