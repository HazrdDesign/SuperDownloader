# Acceptance checklist: Video Downloader

Run these on a Windows 10/11 PC with `dist\VideoDownloader.exe` from `scripts\build.bat` or from the
`VideoDownloader` artifact of the **windows-build** GitHub Actions run.

The **Automated** column says what is already covered without a person. "CI" is the Windows build
job, which runs `scripts/acceptance_smoke.py` against live sites. "pytest" is the local/CI test suite.
Items marked **Manual** need you at the PC.

| # | Test | Automated | Manual steps |
|---|---|---|---|
| 1 | Public YouTube → 🟢 Ready with real qualities, 1080p MP4 to Downloads, Show in folder | CI: check, real quality list, MP4 download with video+audio at the chosen height, `(1)` naming. pytest: Show in folder is called with the saved file | Paste a public YouTube link, choose **1080p**, Download. Check the file is in **Downloads** and plays. Click **Show in folder**: Explorer opens with the file selected. |
| 2 | Public Vimeo behaves the same | CI: same checks as #1 on `vimeo.com/76979871` | Repeat #1 with a public Vimeo link. |
| 3 | Audio only → playable MP3 | CI: ffprobe confirms an MP3 stream and duration. pytest: local MP3 conversion | Choose **Audio only (MP3)**, download, and play it. |
| 4 | Private Vimeo: None → 🟡 Needs login; Zen profile + Retry downloads it | pytest: UI flow (banner, dropdown, Retry passes the Zen profile to yt-dlp); cookie errors map to plain messages | **Manual.** Log into Vimeo in Zen. Paste a private video link with *Use login from* = None: you should see the 🟡 message. Choose **Zen — Default (release)**, click **Retry**: 🟢 Ready. Download it. |
| 5 | Garbage → 🔴 invalid; non-video page → 🔴 unsupported | CI (live `example.com`) and pytest | Type `hello world` and press Enter. Then paste `https://example.com`. |
| 6 | Cancel mid-download leaves no partial files | CI (live Vimeo), pytest (single file and fragmented DASH), and a UI test with the real engine | Start a large download, press **Cancel** (or Esc). The folder has no `.part`/`.ytdl`/`.f137.mp4` leftovers. |
| 7 | Same video twice → `(1)` file | CI (live) and pytest | Download the same video twice: `Title.mp4` and `Title (1).mp4`. |
| 8 | Default folder in Settings persists after restart | pytest: saved via the Settings window, "restart", read back, main window shows it | ⚙ → **Browse** → pick a folder → **Save**. Close and reopen the app: **Save to** shows that folder. |
| 9 | UI never freezes during check or download | pytest: with the real engine, no single UI update takes longer than 250 ms through check, download and cancel | While checking and downloading, move and resize the window and click around. It keeps responding. |

## Also worth a quick look

- **SmartScreen**: the first run shows "Windows protected your PC" → **More info** → **Run anyway**.
- **Window size/position** is remembered between runs. The window can't be made narrower than about 520 px.
- **Ctrl+V** anywhere pastes into the link box and starts a check. **Enter** checks, then downloads. **Esc** cancels.
- **Settings → Check for update** shows "Up to date" (or offers an update and then "Updated — restart the app").
- **Settings → Open logs** opens `%APPDATA%\VideoDownloader\logs\`. Search the log for a password you used: it shouldn't be there.
- **Chrome/Edge/Brave** in *Use login from*: the note about Windows encryption appears. If reading fails, you get
  the plain message instead of an error dump.
