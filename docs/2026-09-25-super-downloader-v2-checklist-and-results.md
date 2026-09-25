# Super Downloader 2.0: checklist and test results (2026-09-25)

This release covers the rename, the dgnl.co theme, the simpler login, and these
features: queue, history, project folders and naming, clip ranges, editing formats (Original kept as the default),
subtitles, the installer, and the conveniences (drag and drop, copied links, a sound and taskbar flash when done, faster chunked downloads).
It also fixes the crash from the user log: `image "pyimage1" doesn't exist`.

## Verified automatically

**Windows build:** [windows-build #10](https://github.com/HazrdDesign/SuperDownloader/actions/runs/36088720178), with the `SuperDownloader` artifact.

| Check | Result |
|---|---|
| Full test suite on Windows (in `build.bat`) and Linux | Passed (Linux: 210 passed, 3 skipped as Windows-only or network) |
| `SuperDownloader-Setup.exe` installs silently for the current user, adds a Start-menu shortcut, and the installed app passes its self-test | Passed |
| Start-up: installed or folder build vs portable exe (the self-test run includes downloads and a conversion) | 3.7–3.9 s vs 8.6 s |
| Sizes | Setup 107.8 MB, Portable 146.0 MB |
| Inside the built app, offline: Original MP4, MP3 and Edit-ready MP4 (proves the bundled FFmpeg has H.264), main and Settings windows open, drag-and-drop library loads | Passed (`ok: original, mp3, edit`; `drag and drop: 2.10.2`) |
| The built app loads an updated engine from `%APPDATA%\SuperDownloader\engine` | Passed |
| Formats against real media (local server): edit-ready is H.264 at constant frame rate; ProRes is `prores` with PCM audio at constant frame rate; WAV; full-resolution JPG thumbnail; clip length; subtitles saved as `.srt`; project file-name prefix; cancelling during a conversion leaves no files | Passed |
| Queue: runs in order, cancel running or waiting items, batch paste, re-downloading after it goes idle | Passed |
| History: green and red rows, cancelled sessions left out, click re-downloads with the same format and subtitles, persists, Clear | Passed |
| Login: automatic browser choice (never Chrome), automatic retry when a site asks for a login, Vimeo checkbox ticked by default, "Never" setting, Chrome note | Passed |
| Settings upgrade from version 1 (`none` login becomes automatic; the old "audio" quality becomes the MP3 format); `%APPDATA%\VideoDownloader` copied to `%APPDATA%\SuperDownloader` | Passed |
| Thumbnail crash from the user log (re-check after a thumbnail was shown) | Regression test passes |

**Bugs found by the Windows build and fixed before release:**
- Clipping failed on PCs without FFmpeg on PATH. yt-dlp's "partial download" check ignores the FFmpeg location it's given, so the bundled FFmpeg is now put on the app's PATH.
- An old link already on the clipboard was picked up when the app started. Now only links copied while the app is open are used.
- The build script's tool check could fail by accident because of how PowerShell handled the tool's output.

**Live sites from GitHub's servers:** refused, as before. YouTube asks "confirm you're not a bot", and Vimeo requires a login
or offers only DRM streams. The app showed the right messages. These have to be checked by hand from a normal connection (below).

## Manual checklist on your PC

1. **Install:** run `SuperDownloader-Setup.exe` (SmartScreen: *More info → Run anyway*). Open it from the Start menu. It should
   open in about a second, and your old settings (default folder, Zen) should still be there.
2. **Vimeo (public and private):** paste a Vimeo link. *Log in with Zen* should show and be ticked, and it should reach Ready without any choices.
3. **Another site that needs a login** (for example an age-restricted YouTube video): it should reach Ready without asking. The login row appears already ticked.
4. **Formats:** download the same short video as Original, Edit-ready, ProRes, MP3, WAV and JPG. Drop the Edit-ready and ProRes
   files into Premiere or Resolve.
5. **More options:** clip `0:10` to `0:20` and check the file is about 10 s long. Download subtitles and check an `.srt` sits next to the video.
   Set a project like `Test / Round 1` and check the folder and name.
6. **Queue:** start a download, paste another link, press Download, and check it shows *Added to the queue*. Also paste 3 links at once.
7. **History:** check a failed download is red. Click a green row and check the options come back. Try right-click → Copy link.
8. **Conveniences:**
   - Drag a link from the browser's address bar onto the window.
   - Copy a link in the browser, click the app, and check the link is picked up.
   - Download something, switch to another app, and check you hear the sound and see the taskbar flash when it finishes.
9. **Brand colors** (background `#232323`, white text, accent `#FF3B33`): check that the Download, Retry and Save
   buttons, the progress bar, the ticks and the taskbar icon are red, and the other buttons are outlined.
