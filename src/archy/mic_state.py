"""Whether the microphone is open, remembered across restarts.

Turning the microphone off is a decision, usually because the room is not
private. It should not be silently undone by a restart.
"""
from __future__ import annotations

import json

from . import atomic, config


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
    """Record the switch, in one step, so a crash cannot leave half a file.

    The staging file is not a fixed "microphone.tmp": two writers share that name,
    and one of them installing it while the other is halfway through writing it
    leaves a truncated file, which reads as on. A privacy switch that fails open
    is worse than one that fails loudly.
    """
    try:
        atomic.write(path(), json.dumps({"enabled": bool(enabled)}))
    except OSError as exc:
        print(f"[mic] could not record the microphone state: {exc}")
