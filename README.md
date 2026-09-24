# Video Downloader

A simple Windows app for downloading videos. Open it, paste a link, see whether the link
will work, pick a quality, and download it to your Downloads folder. It's a single `.exe`
with nothing to install. Behind the scenes it uses [yt-dlp](https://github.com/yt-dlp/yt-dlp).

- Paste a link. The app checks it right away and tells you in one plain sentence whether it will work.
- The quality choices come from what the video actually has (4K, 1080p, 720p… or Audio only as MP3).
- Files are MP4 (or MP3), named after the video. Nothing is ever overwritten: a second copy
  becomes `Title (1).mp4`.
- Private or members-only videos can use the login already saved in Firefox, Zen, LibreWolf or
  Floorp. Your password is never seen or stored.
- Copy-protected (DRM) videos are detected and not downloaded. The app never tries to get around DRM.

---

## Using the app

1. **Paste a link** into the box (Ctrl+V or the **Paste** button). Checking starts on its own.
   You can also press **Check** or Enter.
2. Read the colored message:

   | Message | What it means / what to do |
   |---|---|
   | 🟢 **Ready to download** | You'll see the thumbnail, title, uploader and length. |
   | 🟡 **Private or members-only** | Log into the site in your browser, pick that browser under **Use login from**, then **Retry**. |
   | 🟡 **Needs a password** | Only if you were given the password: type it in and **Retry**. |
   | 🟡 **Only plays on a specific website** | Enter the address of the page the video plays on under **Page it plays on**, then **Retry**. |
   | 🔴 **Copy-protected** | This video can't be downloaded. |
   | 🔴 **Not a web address / no video found / not available** | Check the link. |
   | 🔴 **Couldn't connect** | Check your internet connection. **Details** shows the technical error. |

3. Pick a **Quality**. The size shown is an estimate. The default comes from Settings.
4. **Save to** shows where the file will go. Use **Change** to pick a different folder for this download only.
5. Press **Download** (or Enter). You'll see progress, speed and time left. **Cancel** (or Esc)
   stops the download and deletes the partial file.
6. When it's done, **Show in folder** opens Explorer with the file selected. **Download another** starts over.

If the link is part of a playlist, the app asks whether you want **just this video** (the default) or **all N videos**.
All the videos go into a folder named after the playlist.

### Keyboard

| Key | Action |
|---|---|
| Ctrl+V | Paste into the link box and check |
| Enter | Check the link, or Download once it's ready |
| Esc | Cancel the download (or stop a check) |

### Settings (⚙)

- **Default save folder**: your Downloads folder unless you choose another. The app checks that the folder exists and can be written to.
- **Default quality**: Best available, 1080p, 720p or Audio only. If a video doesn't have that quality, Best is used.
- **Default login source**: None, or one of the browser profiles found on this PC.
- **Downloader engine**: shows the yt-dlp version. **Check for update** downloads a newer engine if there is one; restart the app to use it.
- **Check for engine updates when the app starts** (on by default). This check is silent unless an update exists.
- **Open logs**: opens the folder with the app's log files, which are useful when reporting a problem.

Settings are stored in `%APPDATA%\VideoDownloader\settings.json`. Logs are in `%APPDATA%\VideoDownloader\logs\`.
Passwords and cookie values are never written to the logs.

---

## First run: the Windows SmartScreen warning

The exe isn't code-signed, so the first time you open it Windows may show
**"Windows protected your PC"**. To run it:

1. Click **More info**.
2. Click **Run anyway**.

Windows remembers this, so you only need to do it once for each copy of the exe. Some antivirus
programs are also cautious about new unsigned programs. If yours blocks it, allow `VideoDownloader.exe`.

The first launch takes a few seconds because the app unpacks its bundled parts (FFmpeg and Deno).

---

## Troubleshooting

**"This video is private or members-only"**
1. Open the site in Firefox, Zen, LibreWolf or Floorp and make sure you're logged in and can play the video.
2. In the app, choose that browser under **Use login from** and click **Retry**.

**"Couldn't read your login from … Close … completely"**
The browser is holding its login database open, or the login has expired. Log in again, **close the
browser completely** (check the system tray), then click **Retry**.

**"The login from … didn't work"**
Your login in that browser may have expired, or you may be using a different browser profile.
Log in again in that browser, close it, and retry. If you have several profiles, pick the one you logged in with.

**Chrome, Edge or Brave**
These are listed, but Windows encryption usually blocks other programs from reading their logins. For private
videos, log in with Firefox, Zen, LibreWolf or Floorp instead.

**A profile that isn't listed**
Choose **Custom profile folder…** and select a Firefox-style profile folder (one that contains `cookies.sqlite`).

**"Couldn't read this page. The website may have changed."** (or downloads from a site suddenly stop working)
Sites change often. Open **Settings → Check for update**, update the downloader engine, and restart the app.

**"Couldn't connect"**
Check your internet connection, VPN or proxy. **Details** shows the exact error.

**Something else**
Open **Settings → Open logs** and include the newest `app.log` when you report the problem.

---

## Building the exe

You need **Windows 10/11** and **Python 3.11 or newer** (from [python.org](https://www.python.org/downloads/),
with "Add python.exe to PATH" ticked). Then double-click:

```
scripts\build.bat
```

It will:

1. create a virtual environment in `.venv` and install the pinned `requirements.txt`
2. run `scripts\fetch_ffmpeg.ps1`, which downloads the pinned **FFmpeg 9.0.2** (essentials) and
   **Deno 2.9.6** builds into `vendor\` and **verifies their SHA-256 checksums**
3. run the tests (`pytest`)
4. build `dist\VideoDownloader.exe` with PyInstaller (`VideoDownloader.spec`, one file, no console)
5. run the exe's headless self-test (engine version, FFmpeg, Deno) and print the result

The output is **`dist\VideoDownloader.exe`**. Copy that single file anywhere. It needs nothing else installed.

GitHub Actions runs the same build on every push (`.github/workflows/windows-build.yml`), then tests
the exe and runs live checks against YouTube and Vimeo. The exe is attached to each run as an artifact.

### Development

```
.venv\Scripts\python -m app.main          # run from source
.venv\Scripts\python -m pytest            # all tests (UI tests need a desktop session)
set VD_NETWORK_TESTS=1                    # also run the test that downloads from PyPI
.venv\Scripts\python scripts\acceptance_smoke.py   # live YouTube/Vimeo checks
```

`VD_APPDATA` points the app at a different settings/log folder (used by the tests). Set `VD_DEBUG=1`
for more detailed logs.

### Project layout

```
app/
  main.py          entry point: logging, engine bootstrap, window; --selftest for CI
  ui_main.py       main window
  ui_settings.py   settings window
  ui_common.py     shared UI helpers (tooltip, colors, Show in folder)
  engine.py        yt-dlp wrapper: check_link, list_qualities, download, cancel
  errors.py        classify_error + Status: every error becomes one plain-English state
  browsers.py      Firefox/Zen/LibreWolf/Floorp profile detection, Chrome/Edge/Brave listing
  settings.py      settings JSON load/save and defaults
  paths.py         %APPDATA% folders, Downloads (Known Folder API), bundled binaries
  updater.py       engine updates from PyPI, loaded at startup
assets/            icon.ico / icon.png (scripts\make_icon.py regenerates them)
vendor/            ffmpeg\ and deno\ (downloaded by the build, not committed)
tests/             pytest suites
scripts/           build.bat, fetch_ffmpeg.ps1, acceptance_smoke.py, make_icon.py
docs/              acceptance checklist and results
```

### How engine updates work

A packaged exe can't `pip install`. Instead, **Update** downloads the newest yt-dlp release and the
matching `yt-dlp-ejs` from PyPI as pure-Python wheels into `%APPDATA%\VideoDownloader\engine\` and checks
them against the SHA-256 published by PyPI. On the next start, the app adds those wheels to the front
of Python's import path, before anything loads yt-dlp. If the downloaded engine is missing, altered,
fails to import, or is older than the one built into the exe, the app uses the built-in engine instead.
It also remembers a version that failed so it doesn't offer it again.

---

## Third-party software

- **yt-dlp** and **yt-dlp-ejs**: Unlicense
- **FFmpeg** 9.0.2 "essentials" build by Gyan Doshi ([gyan.dev](https://www.gyan.dev/ffmpeg/builds/)):
  GPL v3. The license is included in the build (`vendor\ffmpeg\LICENSE`). Source code:
  [ffmpeg.org](https://ffmpeg.org/download.html) and the build page linked above.
- **Deno**: MIT
- **CustomTkinter**: MIT. **Pillow**: MIT-CMU.

Use this app only for videos you have the right to download. It does not remove or bypass copy
protection (DRM) and never will.
