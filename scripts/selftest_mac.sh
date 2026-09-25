#!/bin/bash
# Runs the built app's "--selftest <report>" with a time limit and prints the report.
# Usage: scripts/selftest_mac.sh [path/to/Super Downloader.app] [report.json] [timeout seconds]
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP="${1:-$ROOT/dist/Super Downloader.app}"
REPORT="${2:-$ROOT/build/selftest-mac.json}"
LIMIT="${3:-300}"
mkdir -p "$(dirname "$REPORT")"
rm -f "$REPORT"
start=$(date +%s)
"$APP/Contents/MacOS/SuperDownloader" --selftest "$REPORT" &
pid=$!
( sleep "$LIMIT"; kill -9 "$pid" 2>/dev/null && echo "Self-test timed out after $LIMIT s." ) &
watchdog=$!
wait "$pid"; code=$?
kill "$watchdog" 2>/dev/null
echo "Self-test finished in $(( $(date +%s) - start )) s with exit code $code"
if [[ -f "$REPORT" ]]; then cat "$REPORT"; else echo "No report was written."; fi
exit $code
