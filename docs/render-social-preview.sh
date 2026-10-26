#!/usr/bin/env bash
# Render docs/social-preview.png, the card GitHub shows when the repository is
# linked somewhere: Discord, Reddit, X, Slack. GitHub's social preview is
# 1280x640 and it is uploaded by hand in Settings, because there is no public API
# for it. This script exists so the card is a build product rather than a binary
# nobody can change.
#
# It uses the standard theme screenshot, so run render-screenshots.sh first.
#
#   ./docs/render-social-preview.sh
set -euo pipefail
cd "$(dirname "$0")/.."

command -v magick >/dev/null || { echo "need magick on PATH"; exit 1; }
test -s docs/theme-standard.png || { echo "run docs/render-screenshots.sh first"; exit 1; }

# Colours are sampled from the dashboard: background #0b1218, accent #4a7989.
magick -size 1280x640 xc:'#0b1218' \
  -fill '#4a7989' -draw 'rectangle 0,0 1280,4' \
  \( docs/theme-standard.png -resize 650x \) -gravity northwest -geometry +600+119 -composite \
  -font DejaVu-Sans-Bold -pointsize 130 -fill '#f0f4f7' -gravity northwest -annotate +56+142 'archy' \
  -font DejaVu-Sans -pointsize 30 -fill '#a9bdc9' -gravity northwest -annotate +62+320 'A voice assistant that' \
  -annotate +62+358 'runs on your own machine.' \
  -font DejaVu-Sans -pointsize 22 -fill '#65828f' \
  -annotate +62+424 'local speech in, local speech out' \
  -annotate +62+460 'sweep, brief, and look at the screen' \
  -annotate +62+496 'no account, no key, no sign-up' \
  -font DejaVu-Sans-Bold -pointsize 23 -fill '#4a7989' -annotate +62+580 'github.com/Dopethedev/archy' \
  docs/social-preview.png

magick identify docs/social-preview.png
