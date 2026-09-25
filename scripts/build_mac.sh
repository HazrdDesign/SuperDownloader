#!/bin/bash
# Builds "Super Downloader.app" for Apple silicon Macs and packs it into a .dmg.
#
# Needs macOS 11+ on an Apple silicon Mac and Python 3.11+ from python.org (it includes Tk,
# which the window needs; Homebrew's Python may not). Steps: create .venv, install the pinned
# requirements, fetch FFmpeg and Deno (scripts/fetch_tools_mac.sh), run the tests, build with
# PyInstaller, self-test the app, then make dist/SuperDownloader-mac.dmg.
#
# The app isn't signed with an Apple Developer ID yet: the first time, macOS says it can't check
# it. Open it anyway from System Settings -> Privacy & Security -> Open Anyway.
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"

PY="${PYTHON:-}"
if [[ -z "$PY" ]]; then
    for c in python3.13 python3.12 python3.11 python3; do
        if command -v "$c" >/dev/null && "$c" -c 'import sys, tkinter; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
            PY="$(command -v "$c")"; break
        fi
    done
fi
[[ -n "$PY" ]] || { echo "Python 3.11+ with Tk not found. Install it from https://www.python.org/downloads/macos/"; exit 1; }
echo "== Using $PY ($("$PY" --version))"

[[ -x .venv/bin/python ]] || "$PY" -m venv .venv
.venv/bin/python -m pip install --quiet --upgrade pip
.venv/bin/python -m pip install --quiet -r requirements.txt

echo "== Fetching FFmpeg and Deno"
scripts/fetch_tools_mac.sh

echo "== Tests"
if [[ "${SKIP_TESTS:-}" != "1" ]]; then .venv/bin/python -m pytest -q; fi

echo "== Building the app"
rm -rf "build/SuperDownloader" "dist/Super Downloader.app" "dist/SuperDownloader-mac"
.venv/bin/python -m PyInstaller --noconfirm --clean SuperDownloader.spec
APP="dist/Super Downloader.app"
# PyInstaller signs everything ad hoc (Apple silicon won't run unsigned code). Re-sign the whole
# bundle so the bundled FFmpeg and Deno are covered, then check the signature holds together.
codesign --force --deep --sign - "$APP"
codesign --verify --deep --strict "$APP"

echo "== Self-test"
scripts/selftest_mac.sh "$ROOT/$APP" "$ROOT/build/selftest-mac.json"

echo "== Disk image"
VERSION="$(.venv/bin/python -c 'from app import __version__; print(__version__)')"
STAGE="build/dmg"
rm -rf "$STAGE" && mkdir -p "$STAGE"
ditto "$APP" "$STAGE/Super Downloader.app"
ln -s /Applications "$STAGE/Applications"   # drag the app onto this to install it
DMG="dist/SuperDownloader-mac.dmg"
rm -f "$DMG"
for attempt in 1 2 3 4; do   # hdiutil sometimes reports "Resource busy" on build machines
    if hdiutil create -volname "Super Downloader $VERSION" -srcfolder "$STAGE" -ov -format UDZO "$DMG"; then break; fi
    [[ $attempt == 4 ]] && exit 1
    sleep $((attempt * 3))
done
echo "== Done: $DMG ($(du -h "$DMG" | cut -f1))"
