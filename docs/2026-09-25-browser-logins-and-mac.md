# Browser logins: try each browser until one works (2026-09-25)

## What changed

Users no longer choose a browser. When a site needs a login, the app tries the login saved in each browser in turn
and uses the first one that works.

**Order:**
1. The browser that worked for this site last time (remembered per site, e.g. `vimeo.com`).
2. The rest, most recently used first. This is judged by when each browser last wrote its cookie file.
3. Browsers not used in 60 days are skipped (unless there's nothing else). This avoids a Mac permission prompt for a
   forgotten browser.
4. At most 6 browsers are tried for one link.

On Windows, Chrome-based browsers go last, and a browser that couldn't be read goes last for the rest of the session.

**When it's tried:**
- **Vimeo links:** the checkbox *Log in with my browser* is ticked. The app tries each browser, then tries without a
  login, so public videos still work when no browser can be read.
- **Other sites:** the app tries without a login first. It only tries browsers if the site asks for a login.
- A problem a login can't fix (network, password, copy protection, removed video) stops the chain at once.

**While checking**, the progress line says *Trying your login from Chrome…*. After a successful check,
*Using your login from Arc.* appears under the checkbox, and the download uses that login.

**If nothing works**, one message names what was tried. For example: *"The site wants you to sign in first. Your login from
Zen didn't work for it. Windows blocks logins from Chrome and Edge. Log into the site in Zen, make sure the video
plays, then click Retry."* **Details** lists each attempt's raw error.

**Settings → Browser for logins** still offers the following. Choosing one browser means only that one is tried.
- *Automatic: try each browser (…)* (the default)
- a single browser profile
- a custom profile folder
- *Never*

## Which browsers can be read

| Browser | Windows | Mac |
|---|---|---|
| Firefox, Zen, LibreWolf, Floorp | ✅ | ✅ |
| Chrome, Edge, Brave, Vivaldi, Opera | ❌ App-Bound Encryption (Chrome 127+) blocks it; fails in under a second | ✅ macOS asks once for "*Chrome* Safe Storage"; choose **Always Allow** |
| Arc | ❌ same as Chrome | ✅ Keychain entry "Arc Safe Storage" (not yet tested on a real Mac) |
| Safari | — | ✅ with **Full Disk Access** for Super Downloader (System Settings → Privacy & Security) |

We don't work around Chrome's Windows encryption. That's out of scope for this app, and decrypting it is what password-stealing
malware does. On Windows, a Chrome- or Arc-only user will still see the message above for sites that need a login.
The fix for them would be a sign-in window inside the app, which would keep its own login. That's not built yet.

Arc isn't a browser yt-dlp knows by name. `engine.register_extra_browsers()` adds it by giving yt-dlp Arc's folder
and Keychain name. If a future engine update changes how yt-dlp handles browsers, Arc is skipped with a log line and the other
browsers are still tried.

## Checked automatically

- Detection on Windows and Mac folder layouts: Firefox family; Chrome/Edge/Brave/Vivaldi/Opera using the newest profile;
  Arc (including Windows' Store-style folder); Safari.
- Order: most recent first, remembered site first, unused browsers skipped, Chrome-based last on Windows,
  unreadable ones demoted, capped at 6.
- Engine chain: tries in order and skips duplicates. It stops on non-login problems, reports each attempt (`on_attempt`), and
  records which ones failed and why.
- Arc registration with the real yt-dlp: accepted, fails the normal way when not installed. Safari on a non-Mac is
  reported as "couldn't read", not as a crash.
- Messages for Windows and Mac, including the combined "none worked" message.
- UI:
  - Automatic retry across browsers, with the spinner text.
  - The remembered site login is saved to settings and used for the download.
  - Vimeo tries each browser, then no login.
  - *Never* setting; a single browser chosen in Settings; no browsers found.

## To check by hand on your PC (Windows)

1. Log out of Vimeo in Zen, keep Chrome logged in, and paste a Vimeo link that needs a login. The message should say
   Zen's login didn't work and Windows blocks Chrome's.
2. Log into Vimeo in Zen, click **Retry**. It should reach Ready with *Using your login from Zen.*
3. Paste another Vimeo link. Zen should be tried first (no *Trying…* line).

## Still to do for the Mac version

The code already handles Mac paths, *Show in folder* (`open -R`), ⌘V, the Downloads folder and browser locations.
Still needed:
- **Build:** a PyInstaller `.app` for Apple silicon and Intel (`--windowed`, `.icns` icon), built on a Mac or a
  GitHub `macos-latest` runner.
- **Bundled tools:** Mac builds of FFmpeg/FFprobe and Deno, with pinned checksums (`fetch_ffmpeg` for macOS).
- **Signing:** an Apple Developer ID certificate, then signing and notarization. Without them, macOS Gatekeeper blocks the app
  ("can't be opened because Apple cannot check it"), much like SmartScreen does on Windows.
- **Installer:** a `.dmg` with a drag-to-Applications window instead of Inno Setup.
- **Test on a real Mac:** Chrome and Arc Keychain prompts, Safari with Full Disk Access, drag and drop (tkdnd has a
  Mac build), and the done sound (the window bell; there's no taskbar flash on a Mac).
- **Engine updates:** they work the same way (`~/Library/Application Support/SuperDownloader/engine`).
