#!/usr/bin/env bash
# Render docs/theme-*.png without a live assistant.
#
# The dashboard is a static page plus an event stream, so a screenshot needs
# three things: the page, a fake stream that delivers one short transcript
# instantly, and a browser that can screenshot headlessly. The fake stream is
# what keeps this deterministic: no server, no waiting, no animation caught
# mid-frame.
#
#   ./docs/render-screenshots.sh
set -euo pipefail
cd "$(dirname "$0")/.."

for tool in firefox python3; do
  command -v "$tool" >/dev/null || { echo "need $tool on PATH"; exit 1; }
done

work=$(mktemp -d)
port=${PORT:-8799}
trap 'rm -rf "$work"; kill ${server:-} ${browser:-} 2>/dev/null || true' EXIT

cp hud/app.js hud/style.css hud/index.html "$work/"

# the page's own event stream, replaced by a scripted one: state, stats, a
# short exchange, and no clock at all
python3 - "$work/index.html" <<'PY'
import sys
path = sys.argv[1]
page = open(path).read()
stub = """<style>/* a still image: no transitions, so a colour change is never caught mid-way */
*{transition:none !important}</style>
<script>
(function () {
  const script = [
    {type: "state", value: "idle"},
    {type: "stats", cpu: 12.4, mem: 43, temp: 49},
    {type: "turn", latency: 1.4},
    {type: "level", mic: 0.05, out: 0},
    {type: "log", who: "sys", text: "listening for the wake phrase"},
    {type: "log", who: "you", text: "hey, brief me"},
    {type: "log", who: "sys", text: "wake phrase"},
    {type: "log", who: "archy", text: "It is four past five on Saturday, twenty degrees and clear in your city, load is under one and the last sweep came back clean."},
    {type: "log", who: "sys", text: "answered in 7.2s"}
  ];
  class FakeStream {
    constructor() {
      window.addEventListener("load", () => {
        script.forEach((m) => this.onmessage({data: JSON.stringify(m)}));
      });
    }
    addEventListener() {}
    close() {}
  }
  window.EventSource = FakeStream;
})();
</script>
<script src="/app.js"></script>"""
page = page.replace('<script src="/app.js"></script>', stub)

# The page here is served as a plain file, so hud.py never gets the chance to put
# the configured phrase in. The images ship with the public default, and this
# asserts it: a screenshot of "__ARCHY_WAKE__" is worse than no screenshot.
page = page.replace("__ARCHY_WAKE__", "hey archy")
assert "__ARCHY_WAKE__" not in page, "the placeholder survived"
open(path, "w").write(page)
PY

python3 -m http.server "$port" --directory "$work" >/dev/null 2>&1 &
server=$!
sleep 1

# firefox --screenshot ignores a relative path (it writes nothing and still
# exits 0), so the destination is absolute
out="$PWD/docs"
profile=$(mktemp -d)
mkdir -p "$out"
for theme in standard stealth combat; do
  firefox --headless --no-remote --profile "$profile" --window-size 1400,880 \
    --screenshot "$out/theme-$theme.png" \
    "http://127.0.0.1:$port/index.html?theme=$theme#noboot" >/dev/null 2>&1
  test -s "$out/theme-$theme.png" || { echo "no image for $theme"; exit 1; }
  echo "docs/theme-$theme.png"
done
