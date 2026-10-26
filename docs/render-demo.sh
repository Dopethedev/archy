#!/usr/bin/env bash
# Render docs/demo.gif and docs/demo.mp4: the real dashboard, mid turn.
#
# The other render script draws the page from a scripted stream, which is right
# for a still. A demo has to be the thing itself, so this one runs the assistant
# and records the page it serves: the turns are typed into the dashboard's own
# text box, which posts to the same /say endpoint the box uses, and the answers
# come back from the assistant. Both turns in it are answered in code, so it needs
# no model chain and no microphone: the voice loop is what needs those.
#
# It records a real run, so it needs what a real run needs: `uv sync`, the voice
# models from fetch_models, and a port that is free. Playwright drives a headless
# browser for the pictures; `uv run --with playwright` fetches it, and its
# chromium has to be installed once with
#   uv run --with playwright python -m playwright install chromium
#
#   ./docs/render-demo.sh
set -euo pipefail
cd "$(dirname "$0")/.."

for tool in ffmpeg python3 curl; do
  command -v "$tool" >/dev/null || { echo "need $tool on PATH"; exit 1; }
done

port=${PORT:-8731}
work=$(mktemp -d)
trap 'rm -rf "$work"; [ -n "${assistant:-}" ] && kill "$assistant" 2>/dev/null || true' EXIT

# A demo of the project, not of somebody's machine: the shipped wake phrase and
# no name in it. Everything else comes from whichever config.local.json is here,
# because the run is real.
python3 - "$work/config.json" <<'PY'
import json, sys
json.dump({
    "WAKE_PHRASE": "hey archy",
    "WAKE_ALIASES": ["archy", "archie", "arky"],
    "USER_NAME": "the owner",
    "DASHBOARD_STATUS": True,
}, open(sys.argv[1], "w"), indent=2)
PY

ARCHY_LOCAL_CONFIG="$work/config.json" uv run python -m archy --greet "" \
  > "$work/assistant.log" 2>&1 &
assistant=$!

for _ in $(seq 1 40); do
  sleep 2
  [ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 2 "http://127.0.0.1:$port/")" = "200" ] && break
done
[ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 2 "http://127.0.0.1:$port/")" = "200" ] \
  || { echo "the dashboard never came up; see $work/assistant.log"; exit 1; }

uv run --with playwright python - "$port" "$work" <<'PY'
import pathlib, sys, time
from playwright.sync_api import sync_playwright

port, work = sys.argv[1], pathlib.Path(sys.argv[2]) / "video"
work.mkdir()
with sync_playwright() as p:
    browser = p.chromium.launch()
    context = browser.new_context(
        viewport={"width": 1280, "height": 800},
        record_video_dir=str(work),
        record_video_size={"width": 1280, "height": 800})
    page = context.new_page()
    page.goto(f"http://127.0.0.1:{port}/", wait_until="load")
    page.wait_for_selector("#say", timeout=20000)
    time.sleep(2.6)                      # the boot film types itself out
    for text, pause in (("hey archy, what windows are open", 3.4),
                        ("archy, what is the time", 3.0)):
        page.fill("#say", text)
        page.press("#say", "Enter")
        time.sleep(pause)
    context.close()                      # the video is written on close
    browser.close()
PY

video=$(ls "$work"/video/*.webm)
ffmpeg -v error -i "$video" -c:v libx264 -pix_fmt yuv420p -crf 20 \
  -movflags +faststart -an -y docs/demo.mp4
ffmpeg -v error -ss 0.5 -t 9.4 -i "$video" \
  -vf "fps=10,scale=800:-1:flags=lanczos,split[s0][s1];[s0]palettegen=max_colors=96[p];[s1][p]paletteuse=dither=bayer:bayer_scale=5" \
  -loop 0 -y docs/demo.gif

for f in docs/demo.gif docs/demo.mp4; do
  test -s "$f" || { echo "no $f"; exit 1; }
  echo "$f  $(du -h "$f" | cut -f1)"
done
