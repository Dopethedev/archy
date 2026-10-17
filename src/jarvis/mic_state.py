"""Whether the microphone is open, remembered across restarts.

Turning the microphone off is a decision, usually because the room is not
private. It should not be silently undone by a restart.
"""
from __future__ import annotations

import json

from . import config


def path():
    """Resolved on use, not at import: a path bound at import time cannot be
    pointed somewhere else, which is how a test ends up writing into the real
    state directory."""
    return config.STATE_DIR / "microphone.json"


def load() -> bool:
    """True when the microphone should be open. Anything unreadable means on.

    A missing file is a fresh install, and a corrupt one is not a reason to sit
    there deaf without saying so.
    """
    try:
        data = json.loads(path().read_text())
    except (OSError, ValueError):
        return True
    enabled = data.get("enabled") if isinstance(data, dict) else None
    return enabled if isinstance(enabled, bool) else True


def save(enabled: bool) -> None:
    """Record the switch, atomically, so a crash cannot leave half a file."""
    try:
        config.STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = path().with_suffix(".tmp")
        tmp.write_text(json.dumps({"enabled": bool(enabled)}))
        tmp.replace(path())
    except OSError as exc:
        print(f"[mic] could not record the microphone state: {exc}")
