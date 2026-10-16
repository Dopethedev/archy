#!/usr/bin/env bash
# Start JARVIS: voice loop plus the dashboard window.
#
#   ./run.sh                 talk to it, dashboard opens in an app window
#   ./run.sh --no-hud        console only
#   ./run.sh --say "hi"      one typed message, no microphone
#   ./run.sh --mic-test      live mic check: watch the level while you talk
set -euo pipefail
cd "$(dirname "$0")"

# only open a window for the normal talking mode
open=()
case " $* " in
  *--mic-test*|*--say*|*--no-hud*) ;;
  *) open=(--open) ;;
esac

exec uv run python -m jarvis "${open[@]}" "$@"
