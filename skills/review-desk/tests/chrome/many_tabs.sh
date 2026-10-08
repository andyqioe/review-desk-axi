#!/bin/bash
# Real-browser check that many desk tabs in one Chrome profile all load (a browser keeps 6 connections per
# host, so desk tabs that each hold an event stream starve the 7th) and that a hidden tab catches up when shown.
# Usage: tests/chrome/many_tabs.sh [skill dir] [tab count]   (needs Google Chrome and Node 22+)
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
DIR=${1:-$(cd "$HERE/../.." && pwd)}; COUNT=${2:-8}
T=$(mktemp -d)
cleanup() { if [ -n "${CH:-}" ]; then kill "$CH" 2>/dev/null || true; wait "$CH" 2>/dev/null || true; fi; rm -rf "$T"; }
trap cleanup EXIT
free() { python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])'; }
PORT=$(free); CPORT=$(free)
git -C "$T" init -q repo && echo a > "$T/repo/a.txt" && git -C "$T/repo" add a.txt
git -C "$T/repo" -c user.email=t@t -c user.name=t commit -qm init && echo b >> "$T/repo/a.txt"
export REVIEW_DESK_HOME=$T/home REVIEW_DESK_PORT=$PORT REVIEW_DESK_NO_OPEN=1
URL=$(python3 "$DIR/scripts/review_desk.py" open --title "tab test" --repo "$T/repo" | sed -n 's/^url: "\(.*\)"/\1/p')
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless=new --user-data-dir="$T/chrome" \
  --remote-debugging-port="$CPORT" --no-first-run --no-default-browser-check about:blank >/dev/null 2>&1 &
CH=$!
for _ in $(seq 50); do curl -s "http://127.0.0.1:$CPORT/json/version" >/dev/null && break; sleep 0.2; done
node "$HERE/tabs.mjs" "$URL" "$COUNT" "$CPORT"
kill "$(curl -s "http://127.0.0.1:$PORT/health" | python3 -c 'import json,sys; print(json.load(sys.stdin)["pid"])')"
