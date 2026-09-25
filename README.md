# Super Downloader

A simple Windows app for downloading videos, made for dgnl.co. Paste a link and the app tells you
right away whether it will work. Pick a quality and a format, then download. It runs on
[yt-dlp](https://github.com/yt-dlp/yt-dlp), with FFmpeg and Deno built in, so there's nothing else
to install.

- **Paste, download.** The link is checked as soon as it's pasted; if there's a problem, one plain sentence says what. The quality list comes from what the
  video actually has (4K, 1080p, 720p…).
- **Formats for editing:**
  - **Original MP4**, the default, with no re-encoding
  - **Edit-ready MP4**: constant frame rate H.264, so Premiere and Resolve handle it cleanly
  - **ProRes 422 HQ (.mov)**
  - **MP3** or **WAV** audio
  - the **full-resolution thumbnail** as a JPG
- **Queue.** Keep pasting while something downloads, or paste a whole list of links at once.
- **History.** Every past download is one compact line; failed ones are red. Click one to download it again with the same settings.
- **More options** (hidden until you need them):
  - **Clip** a time range.
  - Save **subtitles** as `.srt`.
  - Set a **Client / Project**, which picks the folder and the file-name prefix.
- **Private videos:** the app tries the login saved in each of your browsers, the one you used most recently
  first, until one works. It remembers which one worked for each site. Your password is never seen or stored.
  Copy-protected (DRM) videos are detected and never downloaded.

---

## Installing

Download from the latest **windows-build** run on GitHub (Actions → newest green run → *Artifacts* →
**SuperDownloader**) and unzip it:

| File | Use it when |
|---|---|
| **SuperDownloader-Setup.exe** | You want it installed. It starts in about a second and adds a Start-menu shortcut. No admin rights are needed. |
| **SuperDownloader-Portable.exe** | You want a single file to carry around. It unpacks itself each time it starts, which takes a few seconds. |

**Windows SmartScreen.** The app isn't code-signed yet, so the first time Windows may say
"Windows protected your PC". Click **More info**, then **Run anyway**. Windows remembers this for that file.

Upgrading from *Video Downloader*? Your settings, your default login browser and any updated engine are copied over
the first time Super Downloader starts.

---

## Using the app

1. **Paste a link** with Ctrl+V, drag it onto the window, or copy it in your browser and switch to the app, which
   picks it up for you. The check starts on its own. When the link works, the video's title and thumbnail appear.
2. If there's a problem, a message says what to do (the color shows whether you can fix it):

   | Message | What to do |
   |---|---|
   | 🟡 **The site wants you to sign in / private video** | Log into the site in your browser and click **Retry**. The app tries each browser's login by itself. |
   | 🟡 **Needs a password** | Only if you were given the password: type it in and click **Retry**. |
   | 🟡 **Only plays on a specific website** | Enter the page it plays on and click **Retry**. |
   | 🔴 **Copy-protected** | This video can't be downloaded. |
   | 🔴 **Not a web address / no video / not available / couldn't connect** | Check the link or your connection, then click **Try again**. **Details** has the technical error. |

3. **Quality**, **Format** and **Download** sit on one row. Quality is Best or any height the video really has; Format is
   described above (hover over it for a reminder).
4. **More options ▸**
   - *Only part of the video*: enter a start and end time such as `1:30` and `2:05`.
   - *Subtitles*: a language the video has, saved next to the video as `.srt`.
   - *Project*: for example `Nike / Spring 2027`. The file goes into `…\Nike\Spring 2027\` and is named
     `Nike_Spring-2027_2026-09-25_Title.mp4`. Recent projects are remembered.
5. **Download** (or press Enter). The progress shows percent, speed and time left. **Cancel** (or Esc) removes partial files.
   If you paste another link meanwhile, the download carries on in the **Queue**.
6. When it's done, click **Show in folder**. You'll also hear a sound and the taskbar button flashes if you're in another app.

**Logins, without choosing a browser.** Vimeo links show a single checkbox, *Log in with my browser (for private
or password-protected videos)*, which is already ticked. For any other site the app first tries without a login and
only uses one if the site asks. Either way it tries each browser in turn, the one you used most recently first,
and shows *Trying your login from Chrome…* while it does. The first one that works is used for the download and
tried first for that site next time. If none works, the message says which browsers were tried and why.

| | Windows | Mac (coming later) |
|---|---|---|
| Firefox, Zen, LibreWolf, Floorp | ✅ | ✅ |
| Chrome, Edge, Brave, Arc, Vivaldi, Opera | ❌ Windows blocks reading their logins, so they're tried last | ✅ The Mac asks once: choose **Always Allow** |
| Safari | not on Windows | ✅ Needs **Full Disk Access** for Super Downloader |

**Playlists.** The app asks whether you want just this video (the default) or all of them. A full playlist is saved in a folder named after it.

### Keyboard

| Key | Action |
|---|---|
| Ctrl+V | Paste into the link box and check. Several links go into the queue. |
| Enter | Check the link again, or Download once it's ready |
| Esc | Cancel the download or the check |

### History

Each download session is one line: title on the left; site, format and time on the right. Failed ones are red (hover for
the reason). Cancelled downloads aren't listed.
- **Click** a row to put the link back with the same quality, format, subtitles, clip and project.
- **Show** opens the file in Explorer.
- **Right-click** a row for *Copy link* or *Remove from history*.
- **Clear** empties the list. Your files stay where they are.

### Settings (⚙)

- **Default save folder.** Your Downloads folder unless you choose another.
- **Default quality** and **Default format.**
- **Browser for logins.** *Automatic* (recommended) tries each browser in turn. You can also pick one browser
  profile, a custom profile folder, or *Never*. On Windows, Chrome-based browsers are listed but usually can't be read.
- **Use a video link I just copied** when switching to the app (on by default).
- **Flash the taskbar and play a sound** when a download finishes (on by default).
- **Downloader engine.** Shows the yt-dlp version. **Check for update** gets a newer one; restart the app to use it.
  It can also check automatically when the app starts.
- **Open logs.**

Settings, history and logs are in `%APPDATA%\SuperDownloader\`. Passwords and cookie values are never written anywhere.

---

## Troubleshooting

- **"The site wants you to sign in" or "private video":** open the site in your browser, make sure you're logged in
  and the video plays, then click **Retry**. On Windows that browser must be Firefox, Zen, LibreWolf or Floorp.
- **"Couldn't read your login from … Close … completely":** close that browser fully (check the system tray) and click **Retry**.
- **"The login from … didn't work":** your login there may have expired, so log in again. If you have several browser profiles,
  choose the right one in **Settings → Browser for logins**.
- **Chrome, Edge, Brave or Arc on Windows:** Windows encryption blocks reading their logins; the app tries them anyway and
  moves on. For private videos, log in with a Firefox-family browser.
- **"Couldn't read this page. The website may have changed."** or a site that suddenly stops working: go to **Settings →
  Check for update**, then restart the app.
- **Edit-ready or ProRes takes a while:** those formats re-encode the video on your PC. The progress shows *Converting for editing… %*.
  ProRes files are large, about 10× an MP4.
- **Anything else:** open **Settings → Open logs** and send the newest `app.log`.

---

## Building it yourself

You need **Windows 10/11** and **Python 3.11 or newer** (from [python.org](https://www.python.org/downloads/), with
"Add python.exe to PATH" ticked). For the installer you also need
[Inno Setup 6](https://jrsoftware.org/isinfo.php); without it only the portable exe is built. Then double-click:

```
scripts\build.bat
```

It will:
1. create `.venv` and install the pinned `requirements.txt`.
2. run `scripts\fetch_ffmpeg.ps1`, which downloads the pinned **FFmpeg 9.0.2** and **Deno 2.9.6** builds and checks their SHA-256.
3. run the tests.
4. build both versions with PyInstaller (`SuperDownloader.spec`).
5. self-test both builds. The self-test downloads and converts a local test clip inside the exe, with no internet needed.
6. build `dist\SuperDownloader-Setup.exe` with Inno Setup (`installer\SuperDownloader.iss`).

GitHub Actions (`.github/workflows/windows-build.yml`) does the same on every push. It also installs the
installer, runs the installed app, and runs live checks against YouTube and Vimeo.

### Changing the brand colors

All colors are in **`app/theme.py`**: `BACKGROUND` (`#232323`), `TEXT` (white), `ACCENT` (`#FF3B33`: the main button
on each screen, progress bars, ticks) and `ACCENT_TEXT` (white text on the red). Other buttons are outlined so the
red stays reserved for the main action. Everything else is derived from them. After changing them, run
`python scripts\make_icon.py` to recolor the icon, then rebuild. Only problems get a status color: amber (something
you can fix) and red (failed). Success stays neutral, so the screen doesn't fill up with colors.

### Development

```
.venv\Scripts\python -m app.main          # run from source
.venv\Scripts\python -m pytest            # all tests (UI tests need a desktop session)
set VD_NETWORK_TESTS=1                    # also run the test that downloads from PyPI
.venv\Scripts\python scripts\acceptance_smoke.py   # live YouTube/Vimeo checks
```

`VD_APPDATA` points the app at a different settings folder (the tests use it). `VD_DEBUG=1` writes more detailed logs.

### Project layout

```
app/
  main.py          entry point: logging, settings migration, engine bootstrap, window; --selftest
  ui_main.py       main window: form, queue panel, history panel
  ui_settings.py   settings window
  ui_common.py     tooltip, Show in folder, "done" notification
  theme.py         brand colors → CustomTkinter theme
  engine.py        yt-dlp wrapper: check, qualities, formats, clips, subtitles, download, convert, cancel
  jobs.py          download queue (one at a time, background thread)
  history.py       download history (history.json)
  errors.py        classify_error: every error becomes one plain-English state
  browsers.py      browser detection (Windows and Mac) and the order logins are tried in
  settings.py      settings.json load/save, upgrades from older versions
  paths.py         %APPDATA% folders, Downloads (Known Folder API), bundled binaries, migration
  updater.py       engine updates from PyPI, loaded at startup
assets/            icon (generated from the theme by scripts\make_icon.py)
installer/         Inno Setup script
vendor/            FFmpeg and Deno (downloaded by the build, not committed)
tests/             pytest suites
scripts/           build.bat, fetch_ffmpeg.ps1, selftest.ps1, acceptance_smoke.py, make_icon.py
docs/              acceptance checklists and results
```

### How engine updates work

A packaged app can't `pip install`. Instead, **Update** downloads the newest yt-dlp and the matching `yt-dlp-ejs`
from PyPI as pure-Python wheels into `%APPDATA%\SuperDownloader\engine\` and checks their SHA-256. On the next start,
the app loads them before the built-in engine. If they're missing, altered, broken or older than the built-in
engine, the app uses the built-in one instead.

---

## Third-party software

- **yt-dlp** and **yt-dlp-ejs**: Unlicense
- **FFmpeg** 9.0.2 "essentials" build by Gyan Doshi ([gyan.dev](https://www.gyan.dev/ffmpeg/builds/)): GPL v3. The license
  is included in the build. Source code: [ffmpeg.org](https://ffmpeg.org/download.html).
- **Deno**: MIT. **CustomTkinter**: MIT. **Pillow**: MIT-CMU. **tkinterdnd2 / tkdnd**: MIT / BSD.

Use this app only for videos you have the right to download. It doesn't remove or bypass copy protection (DRM)
and never will.
