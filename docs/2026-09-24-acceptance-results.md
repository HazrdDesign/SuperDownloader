# Acceptance results: Video Downloader (2026-09-24)

## Where things were tested

| Environment | What ran there |
|---|---|
| **Linux dev container** (Python 3.12, FFmpeg 6.1, Xvfb) | Full pytest suite, including real yt-dlp + FFmpeg downloads from a local HTTP server and headless UI tests. It **cannot reach YouTube or Vimeo** (blocked by the container's network policy). |
| **GitHub Actions `windows-latest`** (Windows Server 2025, Python 3.13 chosen by `build.bat`) | `scripts\build.bat` end to end (venv → pip → FFmpeg/Deno download with checksum check → pytest → PyInstaller → exe self-test), the frozen exe's self-tests, the updater against real PyPI, and live YouTube/Vimeo checks (`scripts/acceptance_smoke.py`). |

Latest green Windows run: [windows-build #5](https://github.com/HazrdDesign/SuperDownloader/actions/runs/36039474345).
The exe is attached to the run as the `VideoDownloader` artifact.

### Build facts (Windows run #5)

- `dist\VideoDownloader.exe`: **145.2 MB**, one file, windowed, with the app icon.
- Bundled: FFmpeg/FFprobe **9.0.2** essentials (SHA-256 `60f46726…2047ba`), Deno **2.9.6** (SHA-256 `559b622e…09b67f`),
  yt-dlp **2026.08.19**, yt-dlp-ejs **0.8.0**.
- Exe self-test, cold start to exit: **7.0 s**. The self-test runs these offline inside the frozen exe:
  - an engine check plus MP4 and MP3 downloads of a clip generated with the bundled FFmpeg (`engine_e2e: ok: clip.mp4, clip.mp3`)
  - opening the real main and Settings windows (`ui_window: ok`).
- **Engine update inside the frozen exe:** with a downloaded wheel in `%APPDATA%\VideoDownloader\engine\`, the exe loaded
  yt-dlp from `…\engine\yt_dlp-2026.8.19-py3-none-any.whl\yt_dlp\__init__.py` (`engine_source: updated`).
- Updater tests against real PyPI: 16/16 passed on Windows and on Linux.

## The nine acceptance tests

| # | Test | Result | Evidence |
|---|---|---|---|
| 1 | Public YouTube → Ready, 1080p MP4 to Downloads, Show in folder | **Not verified live. Needs a manual run.** | GitHub's datacenter IPs get *"Sign in to confirm you're not a bot"* from YouTube (2 of 3 test videos). The third, `BaW_jenozKc`, returned *"This video is unavailable"*, which is either the same block or that video. The app showed the correct 🟡 "The site wants you to sign in first…" and 🔴 "isn't available" messages. What was verified offline, with real yt-dlp + FFmpeg: separate video+audio streams merged into an MP4 at exactly the chosen height (720p and 360p DASH, checked with ffprobe), Show in folder called with the saved file (UI test), Downloads resolved through the Known Folder API on Windows (self-test: `C:\Users\runneradmin\Downloads`). |
| 2 | Public Vimeo, same | **Not verified live. Needs a manual run.** | From the CI IP, `vimeo.com/76979871` made yt-dlp 2026.08.19 fall back to its login-only "web" client, and `player.vimeo.com/video/76979871` was only offered DRM streams. The app mapped these to 🟡 sign-in and 🔴 copy-protected, as designed. Whether a home connection behaves differently must be checked by hand. |
| 3 | Audio only → playable MP3 | **Pass (offline); live not verified** | Real yt-dlp + FFmpeg produced a single `mp3` stream longer than 5 s (ffprobe), on Linux and inside the frozen Windows exe. |
| 4 | Private Vimeo: None → 🟡 Needs login; Zen profile + Retry downloads it | **UI flow pass; live needs a manual run** | UI test: 🟡 banner, login dropdown and Retry appear, and Retry passes `('firefox', <Zen profile>, None, None)` to yt-dlp. Profile detection tested with mock Firefox/Zen/LibreWolf/Floorp folders. Cookie-read errors (locked, Chrome encryption, missing) become plain messages. |
| 5 | Garbage → 🔴 invalid; non-video page → 🔴 unsupported | **Pass (live on Windows)** | `this is not a link` → "That doesn't look like a web address…". `https://example.com/` → "Couldn't find a video at this link…". |
| 6 | Cancel mid-download leaves no partial files | **Pass (offline, Windows and Linux)** | Single-file and fragmented (DASH) downloads cancelled midway leave the folder exactly as it was. Also covered in the UI with the real engine. **The Windows run caught a real bug here:** a `.part` file stayed behind because the exception's traceback kept yt-dlp's file handle open. Fixed and now passing on Windows. |
| 7 | Same video twice → `(1)` file | **Pass (offline, Windows and Linux)** | Second download saved as `Title (1).mp4`, and the first file is unchanged byte for byte. |
| 8 | Default folder in Settings persists after restart | **Pass (automated, Windows and Linux)** | The UI test saves through the Settings window, closes the app (which also saves window size and position), reloads the settings and opens a new window that shows the folder. |
| 9 | UI never freezes during check or download | **Pass (automated, Windows and Linux)** | With the real engine against a throttled local server, no single UI update took longer than 250 ms through check, download and cancel. On Linux the worst was under 100 ms after the fixes below. |

**Bottom line:** everything that can be tested without a residential internet connection or your browser
profile passes, including on real Windows. Tests 1, 2 and 4 against the real sites, and the live parts of 3, 6 and 7, need the manual
checklist in `docs/2026-09-24-acceptance-checklist.md` on your PC.

## Bugs found by testing and fixed

1. **Size estimates were wrong on Windows.** The offline format selector didn't know where the bundled FFmpeg was, so yt-dlp assumed
   it couldn't merge and chose pre-merged formats. It now gets the same FFmpeg location as the real download.
2. **Leftover `.part` file after cancelling on Windows.** Cleanup now runs after the exception is released and garbage is collected.
3. **UI stalls of up to 0.7 s during the first check** (Python's GIL was busy with background work). Fixed with a 1 ms interpreter switch interval,
   one reusable YoutubeDL for size estimates, and a background warm-up of yt-dlp's URL patterns.
4. **A shadowed Tk method.** The window's `state` attribute hid Tk's `state()`, which CustomTkinter calls. It was renamed.
5. **Tk objects collected on worker threads.** Destroyed windows are now garbage-collected on the UI thread.
6. **An MP3 of a video you already have as MP4 was named `Title (1).mp3`.** It's now `Title.mp3`. `(1)` is only used when the same
   file already exists, or when a leftover file could clash with yt-dlp's temporary files.

## Known limitations

- **Datacenter and VPN IPs:** YouTube and Vimeo may ask for a login even for public videos. The app says so in plain language and
  offers "Use login from". On a normal home connection this is uncommon but possible.
- **Exe size, 145 MB:** mostly FFmpeg (essentials build) and Deno, which YouTube needs. Each launch unpacks them to a temporary folder (about 7 s from launch to exit in the CI self-test).
- **Unsigned exe:** SmartScreen asks once (**More info → Run anyway**). Code signing would remove this.
- **Chrome/Edge/Brave logins** usually can't be read on Windows (app-bound encryption). The app says so and suggests a Firefox-family browser.
