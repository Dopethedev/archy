#!/usr/bin/env bash
# Start JARVIS: voice loop plus the dashboard window.
#
#   ./run.sh              talk to it, dashboard opens in an app window
#   ./run.sh --no-hud     console only
#   ./run.sh --say "hi"   one typed message, no microphone
set -euo pipefail
cd "$(dirname "$0")"
exec uv run python -m jarvis --open "$@"
