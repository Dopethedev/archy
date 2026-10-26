#!/usr/bin/env bash
# Start ARCHY: voice loop plus the dashboard window.
#
#   ./run.sh                 talk to it, dashboard opens in an app window
#   ./run.sh --no-hud        console only
#   ./run.sh --say "hi"      one typed message, no microphone
#   ./run.sh --mic-test      live mic check: watch the level while you talk
#
# Linux and macOS both. On macOS the first run asks for the microphone and the
# first screen question asks for Screen Recording, both under System Settings.
# Windows has run.cmd, where there is no bash.
set -euo pipefail
cd "$(dirname "$0")"

# only open a window for the normal talking mode
open=()
case " $* " in
  *--mic-test*|*--say*|*--no-hud*) ;;
  *) open=(--open) ;;
esac

exec uv run python -m archy "${open[@]}" "$@"
