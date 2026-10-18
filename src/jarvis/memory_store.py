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
            tmp.replace(self.path)          # atomic, so a crash cannot truncate it
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
