#!/bin/bash
# Downloads the pinned third-party tools bundled into "Super Downloader.app" (Apple silicon)
# and checks their SHA-256. Nothing is used unless the checksum matches.
#
#   - FFmpeg and FFprobe: static macOS arm64 builds by Martin Riedl (ffmpeg.martin-riedl.de,
#     listed on ffmpeg.org's download page). GPL v3 with libx264, like the Windows build.
#                                                            -> vendor/ffmpeg/
#   - Deno 2.9.6 from the official @deno/darwin-arm64 npm package. Checked against the
#     registry's sha512 integrity:
#     V8uO1Aolrl/yvdMb5lOtgtigs8YuZBjrOrgviVMiYkQ0swymFfzkae1wZak4Xi24t7vf7d/eiMb/7ryjjVm+tg==
#                                                            -> vendor/deno/
#
# To move to newer builds, change the URL and hash together.
#
# If an FFmpeg URL/hash below is empty, the script finds the newest release on the site instead and
# prints its exact URL and SHA-256 so they can be pinned (the build still runs, with a warning).
#
# Usage: scripts/fetch_tools_mac.sh [--force]
set -euo pipefail

FFMPEG_URL="${FFMPEG_URL:-}"
FFMPEG_SHA256=""
FFPROBE_URL="${FFPROBE_URL:-}"
FFPROBE_SHA256=""
FFMPEG_SITE="https://ffmpeg.martin-riedl.de"

DENO_VERSION="2.9.6"
DENO_URL="https://registry.npmjs.org/@deno/darwin-arm64/-/darwin-arm64-${DENO_VERSION}.tgz"
DENO_SHA256="60321cf03244e275589f0d099575bfc739c72ff5aa6e9d19d47d8f0757c24bad"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENDOR="$ROOT/vendor"
DL="$VENDOR/_downloads"
FORCE="${1:-}"

sha256() { shasum -a 256 "$1" | awk '{print $1}'; }

# fetch NAME URL SHA256 FILE: download (with retries) and verify; an empty SHA256 prints the hash.
fetch() {
    local name="$1" url="$2" want="$3" file="$4"
    mkdir -p "$DL"
    if [[ -z "$url" ]]; then
        echo "error: no URL for $name" >&2; exit 1
    fi
    curl -fL --retry 4 --retry-delay 2 -o "$DL/$file" "$url"
    local got; got="$(sha256 "$DL/$file")"
    if [[ -z "$want" ]]; then
        echo "::warning::$name is not pinned yet. Pin it in scripts/fetch_tools_mac.sh:"
        echo "::warning::  URL:    $url"
        echo "::warning::  SHA256: $got"
    elif [[ "$got" != "$want" ]]; then
        echo "error: $name checksum mismatch: expected $want, got $got" >&2
        rm -f "$DL/$file"
        exit 1
    fi
}

# The newest macOS arm64 *release* download of PROGRAM (ffmpeg or ffprobe) listed on the site.
# Release folders look like .../macos/arm64/<build id>_<version>/<program>.zip.
latest_release() {
    local program="$1" page links url
    page="$(curl -fsSL --retry 4 "$FFMPEG_SITE/")"
    links="$(grep -oE 'href="[^"]+"' <<< "$page" | sed -E 's/^href="//; s/"$//' | grep -iE 'mac' || true)"
    url="$(grep -E "macos/arm64/[^/]*_[0-9]+(\.[0-9]+)*/${program}\.zip$" <<< "$links" | head -1 || true)"
    if [[ -z "$url" ]]; then
        echo "error: no macOS arm64 release of $program found on $FFMPEG_SITE. Links seen:" >&2
        echo "$links" >&2
        exit 1
    fi
    [[ "$url" == http* ]] || url="$FFMPEG_SITE/${url#/}"
    echo "$url"
}

if [[ "$FORCE" == "--force" ]]; then rm -rf "$VENDOR/ffmpeg" "$VENDOR/deno"; fi

# --- FFmpeg + FFprobe ------------------------------------------------------------------------
if [[ ! -x "$VENDOR/ffmpeg/ffmpeg" || ! -x "$VENDOR/ffmpeg/ffprobe" ]]; then
    [[ -n "$FFMPEG_URL" ]] || FFMPEG_URL="$(latest_release ffmpeg)"
    [[ -n "$FFPROBE_URL" ]] || FFPROBE_URL="$(latest_release ffprobe)"
    fetch FFmpeg "$FFMPEG_URL" "$FFMPEG_SHA256" ffmpeg-macos-arm64.zip
    fetch FFprobe "$FFPROBE_URL" "$FFPROBE_SHA256" ffprobe-macos-arm64.zip
    rm -rf "$VENDOR/ffmpeg" && mkdir -p "$VENDOR/ffmpeg"
    ditto -x -k "$DL/ffmpeg-macos-arm64.zip" "$DL/ffmpeg-x"
    ditto -x -k "$DL/ffprobe-macos-arm64.zip" "$DL/ffprobe-x"
    find "$DL/ffmpeg-x" -type f -name ffmpeg -exec cp {} "$VENDOR/ffmpeg/ffmpeg" \;
    find "$DL/ffprobe-x" -type f -name ffprobe -exec cp {} "$VENDOR/ffmpeg/ffprobe" \;
    rm -rf "$DL/ffmpeg-x" "$DL/ffprobe-x"
    chmod +x "$VENDOR/ffmpeg/ffmpeg" "$VENDOR/ffmpeg/ffprobe"
    cp "$ROOT/licenses/GPL-3.0.txt" "$VENDOR/ffmpeg/LICENSE"
    {
        echo "FFmpeg static build for macOS arm64 by Martin Riedl (https://ffmpeg.martin-riedl.de)"
        echo "ffmpeg:  $FFMPEG_URL"
        echo "ffprobe: $FFPROBE_URL"
        "$VENDOR/ffmpeg/ffmpeg" -hide_banner -version | head -1
        echo "Source code: https://ffmpeg.org/download.html"
    } > "$VENDOR/ffmpeg/VERSION.txt"
fi

# --- Deno --------------------------------------------------------------------------------------
if [[ ! -x "$VENDOR/deno/deno" ]]; then
    fetch Deno "$DENO_URL" "$DENO_SHA256" "deno-darwin-arm64-${DENO_VERSION}.tgz"
    rm -rf "$VENDOR/deno" && mkdir -p "$VENDOR/deno"
    tar -xzf "$DL/deno-darwin-arm64-${DENO_VERSION}.tgz" -C "$DL" package/deno
    mv "$DL/package/deno" "$VENDOR/deno/deno" && rm -rf "$DL/package"
    chmod +x "$VENDOR/deno/deno"
    "$VENDOR/deno/deno" --version > "$VENDOR/deno/VERSION.txt"
fi

# --- Check they run and have what the app needs ----------------------------------------------
"$VENDOR/ffmpeg/ffmpeg" -hide_banner -version | head -1
"$VENDOR/ffmpeg/ffprobe" -hide_banner -version | head -1
encoders="$("$VENDOR/ffmpeg/ffmpeg" -hide_banner -encoders)"
for enc in libx264 prores_ks aac libmp3lame pcm_s16le mjpeg; do
    grep -q " $enc " <<< "$encoders" || { echo "error: bundled FFmpeg has no $enc encoder" >&2; exit 1; }
done
head -1 "$VENDOR/deno/VERSION.txt"
echo "Tools ready in $VENDOR"
