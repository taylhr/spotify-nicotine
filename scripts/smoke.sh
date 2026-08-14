#!/usr/bin/env bash
# Live end-to-end smoke test. Run this on the machine where Nicotine+ is
# installed, running, logged in, and has the api-nicotine-plus plugin enabled.
#
# Usage:
#   scripts/smoke.sh "https://open.spotify.com/playlist/YOUR_PLAYLIST_ID"
set -euo pipefail

API_URL="${SPOTIFY_NICOTINE_API_URL:-http://127.0.0.1:12339}"
PLAYLIST="${1:-}"

cd "$(dirname "$0")/.."

PY=".venv/bin/python"
if [ ! -x "$PY" ]; then
  echo "No .venv found — run: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
  exit 1
fi

echo "== 1. Nicotine+ API health =="
curl -sf "$API_URL/health" || { echo; echo "FAIL: plugin not reachable at $API_URL (README Part 4)"; exit 1; }
echo

echo "== 2. Soulseek connection =="
curl -sf "$API_URL/status"
echo

if [ ! -f ".spotify-tokens.json" ]; then
  echo "NOTE: no Spotify authorization yet — run this first (opens a browser):"
  echo "  $PY -m spotify_nicotine auth"
fi

if [ -z "$PLAYLIST" ]; then
  echo "No playlist argument given — API checks passed. To continue:"
  echo "  scripts/smoke.sh \"https://open.spotify.com/playlist/...\""
  exit 0
fi

echo "== 3. Dry run (3 tracks, nothing downloaded) =="
"$PY" -m spotify_nicotine download "$PLAYLIST" --limit 3 --dry-run

echo
echo "== 4. If the matches above look right, run for real: =="
echo "  $PY -m spotify_nicotine download \"$PLAYLIST\" --limit 3"
echo
echo "Then verify manually:"
echo "  - entries appear in the Nicotine+ Downloads tab"
echo "  - the GUI did NOT jump to search tabs while searching"
echo "  - Ctrl+C mid-run, re-run: it resumes without re-queueing"
echo "  - '$PY -m spotify_nicotine status \"$PLAYLIST\"' shows progress"
echo "  - '$PY -m spotify_nicotine resolve \"$PLAYLIST\"' walks low-confidence tracks"
