"""What leaves the machine, and what stays on it.

The assistant keeps facts about its owner, reads files when asked, looks at the
screen, and resumes the conversation it had yesterday. All four of those become
part of a request, and a request goes to somebody else's computer unless the local
model is answering. "It is stored locally" is not the same claim as "it stays
local", and this module exists so the second claim is enforceable rather than
implied.

Every switch defaults to the behaviour the assistant already had, because a privacy
setting that changes what happens on upgrade is not a setting. The switches are
permission for a *remote* model: with the local model answering, memory and files
and pictures never leave the machine in the first place, so nothing here has to
stand in the way.

Enforcement lives where a request is built, not in the prompt. Telling a model not
to mention something is not a privacy control, it is a request, and a request can be
answered differently:

    memory      brain._system_prompt   the facts are simply not in the prompt
    files       the permission gate    file tools are refused before they run
    screens     assistant._process     the picture is not attached
    history     brain.start            a fresh session instead of the last one
    transcripts session_dir            a directory that is emptied, not kept

Two of those are worth being precise about. Refusing a file is not something an
approval can override: an approval says "do this one thing", and this says "this
kind of thing does not go to that model at all", so the gate refuses without
offering one. And a switch that is off is reported as off: `status()` is what the
dashboard and the log read, and it says plainly when nothing is enforced.
"""
from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path

from . import config
from .memory_store import Memory

# The switches, their config names, and the behaviour they had before they existed.
# Defaults are True across the board: every one of these things already happened.
SWITCHES = {
    "online_ai": ("PRIVACY_ONLINE_AI", True),
    "memory": ("PRIVACY_SHARE_MEMORY", True),
    "files": ("PRIVACY_SHARE_FILES", True),
    "screens": ("PRIVACY_SHARE_SCREENS", True),
    "history": ("PRIVACY_SHARE_HISTORY", True),
    "transcripts": ("PRIVACY_KEEP_TRANSCRIPTS", True),
}

# One line per unusable setting, not one per turn: a warning that repeats becomes noise
# and noise is what people stop reading.
_warned: set = set()

_WORDS = {
    "online_ai": "using online AI at all",
    "memory": "sending what I remember about you",
    "files": "sending file contents",
    "screens": "sending screenshots",
    "history": "sending earlier conversation",
    "transcripts": "keeping transcripts on disk",
}


def _value(key: str):
    name, default = SWITCHES[key]
    raw = getattr(config, name, default)
    # A config file is hand-edited, so a string, a number or a typo lands here. The
    # safe read of "I cannot tell" is the default, which is what this did before,
    # and it says so rather than pretending the setting was understood.
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return raw != 0
    if isinstance(raw, str):
        spoken = raw.strip().lower()
        if spoken in ("", "0", "false", "no", "off"):
            return False
        if spoken in ("1", "true", "yes", "on"):
            return True
    if name not in _warned:
        _warned.add(name)
        print(f"[privacy] {name} is {raw!r}, which is not a yes or a no; "
              f"using the default ({'on' if default else 'off'})")
    return default


def switches() -> dict[str, bool]:
    return {key: _value(key) for key in SWITCHES}


def on(key: str) -> bool:
    return _value(key)


def model_is_remote(model: str | None) -> bool:
    """Whether this model is somebody else's computer.

    The local chain is the only thing that runs here, and it is identified the same
    way the brain identifies it when it decides which prompt to send.
    """
    if not model:
        return True                          # unknown is treated as remote
    return model not in tuple(config.PI_MODELS_OFFLINE)


def shares(kind: str, model: str | None) -> bool:
    """Whether this kind of context may go to this model."""
    if not on("online_ai") and model_is_remote(model):
        return False
    if model_is_remote(model):
        return _value(kind) if kind in SWITCHES else True
    return True                              # nothing leaves the machine


def why_not(kind: str) -> str:
    return f"{_WORDS.get(kind, kind)} is switched off for online models"


def set_switch(key: str, value: bool) -> Path:
    """Change a switch and write it down, so it survives a restart.

    Written into `config.local.json` rather than into memory, because a privacy
    choice that lasts until the next restart is not a choice. `config.py` is never
    touched: it holds the defaults and the comments that explain them.
    """
    if key not in SWITCHES:
        raise KeyError(f"no such privacy setting: {key}")
    name, _ = SWITCHES[key]
    path = Path(config.LOCAL_CONFIG_FILE) if hasattr(config, "LOCAL_CONFIG_FILE") else None
    if path is None:
        raise RuntimeError("this install does not say where its local config lives")
    body = {}
    if path.exists():
        try:
            body = json.loads(path.read_text())
        except ValueError:
            body = {}
    if not isinstance(body, dict):
        body = {}
    body[name] = bool(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(body, indent=2) + "\n")
    temporary.replace(path)
    setattr(config, name, bool(value))
    return path


def status() -> dict:
    """The effective state, in one place, for the dashboard and the log."""
    chain = tuple(getattr(config, "PI_MODELS_OFFLINE", ()))
    return {
        "switches": switches(),
        "words": {key: _WORDS[key] for key in SWITCHES},
        "local_chain": list(chain),
        "enforced": True,                    # these are checked in code, not asked for
    }


# ------------------------------------------------------------------- forgetting
# Clearing is the one operation here that cannot be undone by flipping a switch, so
# it keeps a copy first and says what it did. `memory.json.bak` is next to the real
# file, not hidden away, because the point of a backup is that a person can find it.

def memory_facts() -> int:
    try:
        return len(Memory().facts)
    except Exception:
        return 0


def clear_memory(keep_copy: bool = True) -> dict:
    memory = Memory()
    count = len(memory.facts)
    backup = None
    if count and keep_copy:
        backup = Path(f"{config.MEMORY_FILE}.bak")
        try:
            shutil.copy2(config.MEMORY_FILE, backup)
        except OSError:
            backup = None
    memory.facts.clear()
    memory._save()
    return {"cleared": count, "backup": str(backup) if backup else None}


def history_files() -> list[Path]:
    try:
        return sorted(config.SESSION_DIR.glob("*.jsonl"))
    except OSError:
        return []


def clear_history(keep_copy: bool = True) -> dict:
    """Delete the saved conversations. The transcript in the dashboard is separate:
    it is in memory in another process, and clearing this does not clear that."""
    files = history_files()
    backup = None
    if files and keep_copy:
        backup = Path(f"{config.SESSION_DIR}.bak")
        try:
            if backup.exists():
                shutil.rmtree(backup)
            shutil.copytree(config.SESSION_DIR, backup)
        except OSError:
            backup = None
    removed = 0
    for path in files:
        try:
            path.unlink()
            removed += 1
        except OSError:
            continue
    return {"cleared": removed, "backup": str(backup) if backup else None}


def forget_everything() -> dict:
    """What "forget everything" means: the facts and the saved conversations."""
    return {"memory": clear_memory(), "history": clear_history()}


# ---------------------------------------------------------------- spoken control
# Clearing cannot be undone, so the words for it are narrow and it takes two of
# them: the request, then a confirmation. "Yes" alone is not a confirmation, for
# the same reason it is not an approval.

_CLEAR_PHRASES = (
    "forget everything", "forget everything you know", "forget everything about me",
    "delete everything you know", "erase everything you know", "clear your memory",
    "forget all of it", "wipe your memory", "forget everything you know about me",
)
_CONFIRM_PHRASES = ("yes forget everything", "confirm forget everything",
                    "yes clear your memory", "yes wipe your memory",
                    "yes delete everything you know")


def _words(text: str) -> str:
    import re
    return " ".join(re.findall(r"[a-z']+", text.lower())).strip()


def _said(text: str, phrases) -> bool:
    spoken = _words(text)
    if not spoken or len(spoken.split()) > 7:
        return False
    return any(phrase == spoken for phrase in phrases)


class Confirmation:
    """A dangerous request waiting for the word that confirms it.

    Held in memory and expiring, because a confirmation that outlives the moment
    is a confirmation for something else.
    """

    def __init__(self, window_s: float = 60.0):
        self.window_s = window_s
        self._asked: float | None = None

    def ask(self) -> str:
        self._asked = time.time()
        facts = memory_facts()
        things = "thing" if facts == 1 else "things"
        return (f"This deletes {facts} {things} I remember and every saved conversation. "
                "Say yes, forget everything to confirm. It cannot be undone here, "
                "though a copy is kept next to the originals.")

    def waiting(self) -> bool:
        return self._asked is not None and time.time() - self._asked <= self.window_s

    def confirm(self, text: str) -> dict | None:
        if not self.waiting() or not _said(text, _CONFIRM_PHRASES):
            return None
        self._asked = None
        return forget_everything()


def asked_to_clear(text: str) -> bool:
    return _said(text, _CLEAR_PHRASES)


def confirmed_clear(text: str) -> bool:
    return _said(text, _CONFIRM_PHRASES)


_STATUS_PHRASES = (
    "what are you allowed to send", "what do you send online", "what can online models see",
    "what can the model see", "privacy status", "what are your privacy settings",
    "what do you share", "what do you send", "are you sending my data", "what leaves this machine",
)


def wants_status(text: str) -> bool:
    spoken = _words(text)
    return bool(spoken) and len(spoken.split()) <= 8 and any(
        phrase in spoken for phrase in _STATUS_PHRASES)


def spoken_status() -> str:
    current = switches()
    off = [f"{_WORDS[key]}" for key, value in current.items() if not value]
    if not off:
        return ("Nothing is restricted: online models may see what I remember, the "
                "files you ask about, screenshots, and earlier conversation.")
    return "Switched off: " + "; ".join(off) + "."


# ------------------------------------------------------------------- command line

_HELP = """usage: privacy [status|list|set KEY on|off|clear-memory|clear-history]

  status              what is switched on and what is not
  list                every setting with its current value
  set KEY on|off      change one and write it down, so it survives a restart
  clear-memory        delete the stored facts, keeping a copy beside them
  clear-history       delete the saved conversations, keeping a copy
"""


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("status", "state"):
        for key, value in switches().items():
            print(f"{key:12s} {'on ' if value else 'OFF'}  {_WORDS[key]}")
        print(f"\nlocal model: {', '.join(config.PI_MODELS_OFFLINE) or 'none configured'}")
        return 0
    command = args[0]
    if command == "list":
        print(json.dumps(status(), indent=2))
        return 0
    if command == "set" and len(args) >= 3:
        wanted = args[2].strip().lower() in ("on", "true", "yes", "1")
        try:
            path = set_switch(args[1], wanted)
        except KeyError as exc:
            print(exc)
            return 2
        print(f"{args[1]} is now {'on' if wanted else 'OFF'}; written to {path}")
        return 0
    if command == "clear-memory":
        result = clear_memory()
        print(f"cleared {result['cleared']} facts"
              + (f", copy at {result['backup']}" if result["backup"] else ""))
        return 0
    if command == "clear-history":
        result = clear_history()
        print(f"cleared {result['cleared']} saved conversations"
              + (f", copy at {result['backup']}" if result["backup"] else ""))
        return 0
    print(_HELP)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
