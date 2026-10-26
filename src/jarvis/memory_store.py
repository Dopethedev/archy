"""Durable facts.

The session survives a restart, but a session is still just this conversation.
Anything that should outlive it, a codeword, a preference, how someone likes
to be addressed, belongs in a store that is read at startup and written on
request. JSON on disk, no database, because the whole point is that it stays
readable and editable by hand.

Usage from the shell:

    python -m jarvis.memory_store list
    python -m jarvis.memory_store set codeword "purple lantern"
    python -m jarvis.memory_store get codeword
    python -m jarvis.memory_store forget codeword
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from contextlib import contextmanager

try:
    import fcntl                       # Unix
except ImportError:
    fcntl = None                       # type: ignore[assignment]
try:
    import msvcrt                      # Windows
except ImportError:
    msvcrt = None                      # type: ignore[assignment]
from pathlib import Path

from . import config

MAX_KEY = 64
MAX_VALUE = 500


def _lock(fh) -> None:
    """Take an exclusive lock on an open file, on either platform.

    The assistant stores facts by running this module as a separate process, so
    without a lock two writers read the same file and the second one saves its
    stale copy over the first: the fact simply vanishes.

    Windows has no flock, so it locks one byte of its own. That byte has to
    exist: Windows refuses to lock a range it cannot see, so a lock file that
    starts empty raises instead of waiting, and the write never happens. It gets
    a byte first. If locking still fails the write goes ahead without one,
    because the save is atomic and a lost update is a smaller problem than a
    fact that cannot be stored at all.
    """
    try:
        if fcntl is not None:
            fcntl.flock(fh, fcntl.LOCK_EX)
            return
        if msvcrt is not None:
            fh.seek(0, os.SEEK_END)
            if fh.tell() == 0:
                fh.write(b"\0")
                fh.flush()
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
    except OSError:
        pass


def _unlock(fh) -> None:
    try:
        if fcntl is not None:
            fcntl.flock(fh, fcntl.LOCK_UN)
        elif msvcrt is not None:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        pass


def _replace(tmp: Path, target: Path, attempts: int = 10) -> None:
    """Move the staged file into place, waiting out a reader on Windows.

    os.replace is what stops a crash truncating the store, but on Windows it needs
    the destination to be replaceable, and a process that has the file open for
    reading can block it. Six writers at once hit that every time, and the fact
    they were saving simply vanished while the lock was held correctly, because
    the failure was in the save rather than in the serialisation. The window is
    the microseconds another process spends reading, so a short wait clears it.
    """
    for attempt in range(attempts):
        try:
            tmp.replace(target)
            return
        except PermissionError:
            time.sleep(0.05 * (attempt + 1))
    tmp.replace(target)                 # last try: report the error rather than hide it


class Memory:
    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else config.MEMORY_FILE
        self.facts: dict[str, dict] = {}
        self._load()

    # ------------------------------------------------------------------ disk
    def _load(self):
        try:
            if self.path.exists():
                raw = json.loads(self.path.read_text())
                if isinstance(raw, dict):
                    self.facts = {str(k): v for k, v in raw.items() if isinstance(v, dict)}
        except Exception as exc:
            print(f"[memory] could not read {self.path}: {exc}", file=sys.stderr)
            self.facts = {}

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # a unique temporary name: two writers, which happens whenever the
        # assistant stores something while the shell does, must not share one
        # staging file and hand each other half a document
        tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            tmp.write_text(json.dumps(self.facts, indent=2, sort_keys=True))
            _replace(tmp, self.path)        # atomic, so a crash cannot truncate it
        finally:
            tmp.unlink(missing_ok=True)

    @contextmanager
    def _exclusive(self):
        """Serialise writers across processes, and re-read under the lock.

        The assistant stores facts by running this module as a command, so a
        second process is normal. Without this, a writer that read the file
        before another one changed it would save its stale copy over the newer
        one and the change would simply vanish.
        """
        lock_path = self.path.with_name(f".{self.path.name}.lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        # "a+b" rather than "w": truncating a file another process holds a lock on
        # is a sharing problem waiting to happen, and the contents never matter
        with open(lock_path, "a+b") as fh:
            _lock(fh)
            try:
                self._load()
                yield
            finally:
                _unlock(fh)

    # ------------------------------------------------------------------- api
    def set(self, key: str, value: str) -> None:
        key = str(key).strip()[:MAX_KEY]
        value = str(value).strip()[:MAX_VALUE]
        if not key:
            raise ValueError("a fact needs a name")
        with self._exclusive():
            self.facts[key] = {"value": value, "updated": int(time.time())}
            self._save()

    def get(self, key: str) -> str | None:
        entry = self.facts.get(str(key).strip())
        return entry.get("value") if entry else None

    def forget(self, key: str) -> bool:
        with self._exclusive():
            if str(key).strip() not in self.facts:
                return False
            del self.facts[str(key).strip()]
            self._save()
            return True

    def summary(self, limit: int = 40) -> str:
        """One readable line per fact, for putting in a prompt."""
        if not self.facts:
            return ""
        items = sorted(self.facts.items())[:limit]
        return "; ".join(f"{k} = {v.get('value', '')}" for k, v in items)

    def line_count(self) -> int:
        return len(self.facts)


# ------------------------------------------------------------------- intents
# What a person says to have something kept, and to hear it back.
#
# This ran as a command before, which cost three things nobody asked for: a shell
# command for a fact that is already on this machine, a round trip to a relay to
# be told what is in a local file, and, with the permission gate on, an approval
# for every "remember that". None of the three are needed for the shapes below,
# which is nearly everything people actually say.

_SPOKEN_STRIP = " \t\r\n.,!?;:'\""
_RE_REMEMBER = re.compile(
    r"^(?:please\s+)?(?:remember|note|make a note|keep in mind|"
    r"don't forget|do not forget|dont forget)\s+(?:that\s+)?(?P<rest>.+)$", re.I)
# "my router is in the hall" is a name and a value; "I take my coffee black" is not.
_RE_PAIR = re.compile(r"^(?P<key>.+?)\s+(?:is|are|was|were|=|:)\s+(?P<value>.+)$", re.I)
_RE_FORGET = re.compile(
    r"^(?:please\s+)?(?:forget|delete|remove|drop|stop remembering)\s+"
    r"(?:that\s+|about\s+)?(?P<what>.+)$", re.I)
# "forget everything" is the privacy question, which has its own confirmation, and
# so is the long way of saying it: "forget everything you know about me" is not one
# fact with an odd name, which is what this read it as.
_NOT_ONE_FACT = {"it", "this", "that", "them", "those", "data",
                 "everything_about_me", "the_whole_memory"}
_EVERYTHING = {"everything", "all"}


def _slug(text: str) -> str:
    """A stored name for something spoken: "my sister's name" -> sister's_name."""
    words = re.sub(r"[^\w\s'-]", " ", str(text).lower()).split()
    while words and words[0] in ("my", "the", "a", "an"):
        words.pop(0)
    return "_".join(words)[:MAX_KEY].strip("_")


def _readable(key: str) -> str:
    return str(key).replace("_", " ")


def _is_note(key: str, value: str) -> bool:
    """A fact with no name of its own, stored under everything it says.

    "remember that I take my coffee black" has no "is" in it, so there is nothing
    to call it. The sentence is the name and the value, which is fine to store and
    reads badly if it is spoken back as a pair: the value is said on its own.
    """
    return _slug(value) == key


def remember_intent(text: str) -> tuple[str, str] | None:
    """The fact a sentence asks to keep, as (name, value), or None.

    None for a question, because "remember my address?" is someone asking whether
    it was kept, and None for an errand: "remember to call mum at five" is a
    reminder, and storing it as a fact would keep it forever.
    """
    said = (text or "").strip()
    if not said or said.endswith("?"):
        return None
    match = _RE_REMEMBER.match(said.strip(_SPOKEN_STRIP))
    if not match:
        return None
    rest = match.group("rest").strip(_SPOKEN_STRIP)
    if not rest or rest.lower().startswith("to "):
        return None
    pair = _RE_PAIR.match(rest)
    if pair:
        key = _slug(pair.group("key"))
        value = pair.group("value").strip(_SPOKEN_STRIP)
    else:
        key, value = _slug(rest), rest
    if not key or not value:
        return None
    return key, value[:MAX_VALUE]


def recall_intent(text: str) -> bool:
    """Whether a sentence is asking what is already known, rather than telling."""
    words = " ".join((text or "").strip(_SPOKEN_STRIP).lower().split())
    if words in config.MEMORY_RECALL_PHRASES:
        return True
    return words.startswith(config.MEMORY_RECALL_PREFIXES)


def forget_intent(text: str, memory: "Memory") -> str | None:
    """What to say back when a sentence asks to drop one stored fact, else None.

    The name is looked up rather than taken literally: "forget my router location"
    and "forget the router location" both land on router_location, and a name that
    matches nothing is answered here too, because "I have nothing stored as that"
    is not worth a model.
    """
    match = _RE_FORGET.match((text or "").strip(_SPOKEN_STRIP))
    if not match:
        return None
    what = match.group("what").strip(_SPOKEN_STRIP)
    wanted = _slug(what)
    if wanted in _NOT_ONE_FACT or len(wanted) < 3:
        return None
    if _EVERYTHING & set(wanted.split("_")):
        return None
    # "about me" on the end is the same request as "everything": the whole store
    if wanted.endswith(("_about_me", "_about_you")):
        return None
    keys = list(memory.facts)
    exact = next((key for key in keys if key.lower() == wanted), None)
    if exact is None:
        near = [key for key in keys if wanted in key.lower() or key.lower() in wanted]
        if len(near) == 1:
            exact = near[0]
        elif len(near) > 1:
            return ("I have more than one of those: "
                    + ", ".join(sorted(_readable(key) for key in near)[:4])
                    + ". Which one?")
    if exact is None:
        memory._load()                        # another process may have just stored it
        return f"I have nothing stored about {what}."
    memory.forget(exact)
    return f"Forgotten: {_readable(exact)}."


def spoken_remember(key: str, value: str) -> str:
    return "Noted." if _is_note(key, value) else f"Noted: {_readable(key)} is {value}."


def spoken_recall(memory: "Memory", limit: int = 8) -> str:
    """Everything stored, said as sentences rather than as a list of pairs."""
    items = sorted(memory.facts.items())
    if not items:
        return "I have nothing stored about you yet."
    parts = []
    for key, entry in items[:limit]:
        value = str(entry.get("value", ""))
        parts.append(value if _is_note(key, value) else f"{_readable(key)} is {value}")
    said = ", ".join(parts)
    extra = len(items) - limit
    return ("Here is what you have told me: " + said
            + (f", and {extra} more" if extra > 0 else "") + ".")


# ---------------------------------------------------------------------- cli
def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    mem = Memory()
    if not args or args[0] in ("list", "ls", "all"):
        if not mem.facts:
            print("no facts stored")
            return 0
        for key, entry in sorted(mem.facts.items()):
            when = time.strftime("%Y-%m-%d", time.localtime(entry.get("updated", 0)))
            print(f"{key}: {entry.get('value', '')}   ({when})")
        return 0

    command = args[0]
    if command == "set" and len(args) >= 3:
        mem.set(args[1], " ".join(args[2:]))
        print(f"remembered {args[1]}")
        return 0
    if command == "get" and len(args) >= 2:
        value = mem.get(args[1])
        print(value if value is not None else f"nothing stored for {args[1]}")
        return 0 if value is not None else 1
    if command in ("forget", "delete", "rm") and len(args) >= 2:
        print(f"forgot {args[1]}" if mem.forget(args[1]) else f"nothing stored for {args[1]}")
        return 0

    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
